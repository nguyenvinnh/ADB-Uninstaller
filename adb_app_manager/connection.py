from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass

from .adb import AdbClient, AdbError
from .models import ConnectionState, Diagnosis, UsbProblem

SCAN_CACHE_SECONDS = 8.0

# Mã nhà sản xuất USB (VID) thường gặp.
VENDORS = {
    "04E8": "Samsung",
    "2717": "Xiaomi",
    "18D1": "Google",
    "22B8": "Motorola",
    "12D1": "Huawei",
    "0BB4": "HTC",
    "1004": "LG",
    "0FCE": "Sony",
    "2A70": "OnePlus",
    "22D9": "Oppo",
    "2D95": "Vivo",
    "0B05": "Asus",
    "19D2": "ZTE",
    "0E8D": "MediaTek",
    "05C6": "Qualcomm",
}

# Mã lỗi của Windows Device Manager.
DRIVER_ERRORS = {
    1: "thiết bị chưa được cấu hình đúng",
    3: "driver bị hỏng hoặc thiếu bộ nhớ",
    10: "thiết bị không khởi động được",
    22: "thiết bị đang bị vô hiệu hóa",
    28: "chưa cài driver",
    31: "Windows không tải được driver",
    37: "driver không khởi tạo được",
    39: "Windows không tải được driver",
    43: "Windows đã dừng thiết bị vì báo lỗi",
    52: "driver chưa được ký số",
}

# Mã 0: bình thường. Mã 45: thiết bị hiện không cắm.
_IGNORED_CODES = {0, 45}
_ADB_NAME = re.compile(r"adb|android", re.IGNORECASE)
_VID = re.compile(r"VID_([0-9A-Fa-f]{4})")
_VID_PID = re.compile(r"VID_[0-9A-Fa-f]{4}&PID_[0-9A-Fa-f]{4}", re.IGNORECASE)


def vendor_id(device_id: str) -> str | None:
    match = _VID.search(device_id)
    return match.group(1).upper() if match else None


def vendor_name(device_id: str) -> str | None:
    vid = vendor_id(device_id)
    return VENDORS.get(vid) if vid else None


def describe_problem(problem: UsbProblem) -> str:
    label = problem.name
    vendor = vendor_name(problem.device_id)
    if vendor and vendor.lower() not in label.lower():
        label = f"{label} ({vendor})"
    reason = DRIVER_ERRORS.get(problem.code, "thiết bị báo lỗi")
    return f"{label}: {reason} [mã {problem.code}]"


@dataclass(frozen=True)
class UsbScan:
    available: bool
    problems: tuple[UsbProblem, ...] = ()
    adb_interface: bool = False


def _scan_windows_usb() -> UsbScan:
    """Liệt kê thiết bị USB qua PowerShell và tìm thiết bị lỗi driver."""
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Get-CimInstance Win32_PnPEntity "
        "| Where-Object { $_.DeviceID -like 'USB\\VID_*' } "
        "| Select-Object Name,DeviceID,ConfigManagerErrorCode "
        "| ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return UsbScan(False)

    text = result.stdout.strip()
    if result.returncode != 0:
        return UsbScan(False)
    if not text:
        return UsbScan(True)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return UsbScan(False)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return UsbScan(False)

    problems: list[UsbProblem] = []
    seen: set[tuple[str, int, str]] = set()
    adb_interface = False
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or "Thiết bị USB không rõ")
        device_id = str(item.get("DeviceID") or "")
        try:
            code = int(item.get("ConfigManagerErrorCode") or 0)
        except (TypeError, ValueError):
            code = 0
        if code == 0:
            if _ADB_NAME.search(name):
                adb_interface = True
            continue
        if code in _IGNORED_CODES:
            continue
        match = _VID_PID.search(device_id)
        key = (name, code, match.group(0).upper() if match else device_id)
        if key in seen:
            continue
        seen.add(key)
        problems.append(UsbProblem(name, device_id, code))
    return UsbScan(True, tuple(problems), adb_interface)


class ConnectionChecker:
    """Xác định vì sao máy tính chưa làm việc được với điện thoại."""

    def __init__(self, adb: AdbClient) -> None:
        self.adb = adb
        self._scan: UsbScan | None = None
        self._scan_time = 0.0

    def _usb_scan(self, fresh: bool) -> UsbScan:
        if not sys.platform.startswith("win"):
            return UsbScan(False)
        now = time.monotonic()
        if (
            not fresh
            and self._scan is not None
            and now - self._scan_time < SCAN_CACHE_SECONDS
        ):
            return self._scan
        self._scan = _scan_windows_usb()
        self._scan_time = time.monotonic()
        return self._scan

    def diagnose(self, restart: bool = False, fresh: bool = True) -> Diagnosis:
        """Kiểm tra theo thứ tự: ADB -> thiết bị -> cấp quyền -> driver."""
        if restart:
            try:
                self.adb.restart_server()
            except AdbError:
                pass  # lỗi sẽ được báo ở bước liệt kê thiết bị bên dưới

        try:
            devices = self.adb.list_devices()
        except AdbError as exc:
            return Diagnosis(ConnectionState.ADB_ERROR, detail=str(exc))

        if any(device.state == "device" for device in devices):
            return Diagnosis(ConnectionState.READY, devices=devices)

        unauthorized = [d for d in devices if d.state == "unauthorized"]
        if unauthorized:
            return Diagnosis(
                ConnectionState.UNAUTHORIZED,
                devices=devices,
                detail=", ".join(d.serial for d in unauthorized),
            )

        if devices:
            return Diagnosis(
                ConnectionState.OFFLINE,
                devices=devices,
                detail=", ".join(f"{d.serial} ({d.state})" for d in devices),
            )

        # ADB không thấy máy nào: kiểm tra driver của Windows.
        scan = self._usb_scan(fresh)
        if scan.problems:
            return Diagnosis(
                ConnectionState.NO_DRIVER,
                problems=scan.problems,
                adb_interface=scan.adb_interface,
            )
        return Diagnosis(ConnectionState.NO_DEVICE, adb_interface=scan.adb_interface)