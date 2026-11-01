from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass

from .adb import AdbClient, AdbError
from .models import ConnectionState, DeviceInfo, Diagnosis, UsbProblem


SCAN_CACHE_SECONDS = 8.0

# Không tự động restart ADB liên tục.
AUTO_RESTART_COOLDOWN_SECONDS = 20.0

# Số lần quét lại khi thiết bị đang ở trạng thái tạm thời (connecting...).
SETTLE_RETRIES = 3
SETTLE_DELAY_SECONDS = 1.0

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

# Trạng thái ADB chỉ tồn tại thoáng qua.
_TRANSIENT_STATES = {"connecting", "authorizing"}

_ADB_NAME = re.compile(r"\badb\b|android", re.IGNORECASE)
_PHONE_NAME = re.compile(
    r"android|\badb\b|\bmtp\b|phone|mobile|portable|unknown usb|composite|"
    r"bootloader|fastboot|samsung|xiaomi|redmi|huawei|oppo|vivo|realme|"
    r"oneplus|pixel|motorola",
    re.IGNORECASE,
)
_VID = re.compile(r"VID_([0-9A-Fa-f]{4})")
_VID_PID = re.compile(
    r"VID_[0-9A-Fa-f]{4}&PID_[0-9A-Fa-f]{4}",
    re.IGNORECASE,
)


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

    reason = DRIVER_ERRORS.get(
        problem.code,
        "thiết bị báo lỗi",
    )

    return f"{label}: {reason} [mã {problem.code}]"


def _looks_like_phone(name: str, device_id: str) -> bool:
    """Loại các thiết bị USB không liên quan (máy in, webcam...)."""
    return vendor_id(device_id) in VENDORS or bool(_PHONE_NAME.search(name))


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
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            check=False,
            creationflags=getattr(
                subprocess,
                "CREATE_NO_WINDOW",
                0,
            ),
        )
    except (OSError, subprocess.TimeoutExpired):
        return UsbScan(False)

    output = result.stdout.strip()

    if result.returncode != 0:
        return UsbScan(False)

    if not output:
        return UsbScan(True)

    try:
        data = json.loads(output)
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

        # Bỏ qua thiết bị lỗi nhưng không giống điện thoại.
        if not _looks_like_phone(name, device_id):
            continue

        match = _VID_PID.search(device_id)

        key = (
            name,
            code,
            match.group(0).upper() if match else device_id,
        )

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

        # Thời điểm tự động restart ADB gần nhất (chưa từng restart).
        self._last_auto_restart = float("-inf")

        # Đã tự restart trong đợt "ADB không thấy máy" này chưa.
        # Được đặt lại khi ADB thấy thiết bị.
        self._restarted_for_empty = False

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

    def _should_auto_restart(self) -> bool:
        """Kiểm tra đã đủ thời gian để tự khởi động lại ADB chưa."""
        return (
            time.monotonic() - self._last_auto_restart
            >= AUTO_RESTART_COOLDOWN_SECONDS
        )

    def _auto_restart_and_scan(self) -> list[DeviceInfo]:
        """Tự khởi động lại ADB rồi quét thiết bị lần nữa."""
        self._last_auto_restart = time.monotonic()
        self.adb.restart_server()
        return self.adb.list_devices()

    def _restart_after_adb_error(self, original_error: AdbError) -> list[DeviceInfo]:
        """Thử phục hồi khi chính lệnh kiểm tra ADB bị lỗi.

        Không restart nếu chưa hết thời gian cooldown. Nếu restart được,
        thực hiện lại lệnh list_devices đúng một lần.
        """
        if not self._should_auto_restart():
            raise original_error

        try:
            return self._auto_restart_and_scan()
        except AdbError as exc:
            raise AdbError(
                "ADB gặp lỗi khi kiểm tra thiết bị và không thể tự phục hồi.\n"
                f"Lỗi ban đầu: {original_error}\n"
                f"Lỗi sau khi restart: {exc}"
            ) from exc

    def _should_restart_for_empty(self, scan: UsbScan) -> bool:
        """Có nên restart ADB khi nó không thấy máy nào hay không.

        Chỉ restart khi có dấu hiệu ADB bỏ sót máy (Windows đã thấy cổng ADB),
        và chỉ một lần mỗi đợt, để không làm gián đoạn các chương trình khác
        đang dùng ADB khi người dùng đơn giản là chưa cắm máy.
        """
        if self._restarted_for_empty or not self._should_auto_restart():
            return False
        # Không quét được USB (Linux/macOS...): thử một lần mỗi đợt.
        return scan.adb_interface or not scan.available

    def _settle(self, devices: list[DeviceInfo]) -> list[DeviceInfo]:
        """Đợi các trạng thái tạm thời (connecting/authorizing) kết thúc."""
        for _ in range(SETTLE_RETRIES):
            if any(d.state == "device" for d in devices):
                break
            if not any(d.state in _TRANSIENT_STATES for d in devices):
                break
            time.sleep(SETTLE_DELAY_SECONDS)
            try:
                devices = self.adb.list_devices()
            except AdbError:
                break
        return devices

    @staticmethod
    def _classify(devices: list[DeviceInfo]) -> Diagnosis:
        """Phân loại khi ADB đã thấy ít nhất một thiết bị."""
        if any(device.state == "device" for device in devices):
            return Diagnosis(ConnectionState.READY, devices=devices)

        unauthorized = [d for d in devices if d.state == "unauthorized"]
        if unauthorized:
            return Diagnosis(
                ConnectionState.UNAUTHORIZED,
                devices=devices,
                detail=", ".join(d.serial for d in unauthorized),
            )

        no_permissions = [d for d in devices if d.state == "no_permissions"]
        if no_permissions:
            return Diagnosis(
                ConnectionState.ADB_ERROR,
                devices=devices,
                detail=(
                    "Thiếu quyền truy cập USB cho: "
                    + ", ".join(d.serial for d in no_permissions)
                    + ". Trên Linux hãy cấu hình udev rules hoặc thêm người "
                    "dùng vào nhóm plugdev."
                ),
            )

        return Diagnosis(
            ConnectionState.OFFLINE,
            devices=devices,
            detail=", ".join(f"{d.serial} ({d.state})" for d in devices),
        )

    def diagnose(
        self,
        restart: bool = False,
        fresh: bool = True,
    ) -> Diagnosis:
        """Kiểm tra theo thứ tự: ADB -> thiết bị -> cấp quyền -> driver.

        Cơ chế phục hồi:
        - Người dùng yêu cầu restart: restart trước khi kiểm tra.
        - list_devices lỗi: tự restart tối đa một lần khi hết cooldown.
        - ADB không thấy máy: kiểm tra driver USB trước; chỉ restart (một
          lần mỗi đợt) khi Windows đã thấy cổng ADB mà ADB không thấy máy.
        """
        # Người dùng chủ động yêu cầu restart ADB.
        if restart:
            try:
                self.adb.restart_server()
            except AdbError as exc:
                return Diagnosis(
                    ConnectionState.ADB_ERROR,
                    detail=f"Không thể khởi động lại ADB server: {exc}",
                )
            self._last_auto_restart = time.monotonic()
            self._restarted_for_empty = True

        # ---------------------------------------------- kiểm tra ADB
        try:
            devices = self.adb.list_devices()
        except AdbError as exc:
            if restart:
                return Diagnosis(ConnectionState.ADB_ERROR, detail=str(exc))
            try:
                devices = self._restart_after_adb_error(exc)
            except AdbError as recovery_error:
                return Diagnosis(
                    ConnectionState.ADB_ERROR,
                    detail=str(recovery_error),
                )

        devices = self._settle(devices)

        if devices:
            self._restarted_for_empty = False
            return self._classify(devices)

        # ---------------- ADB không thấy máy nào: kiểm tra driver trước
        scan = self._usb_scan(fresh)

        if scan.problems:
            return Diagnosis(
                ConnectionState.NO_DRIVER,
                problems=scan.problems,
                adb_interface=scan.adb_interface,
            )

        auto_restarted = False
        if not restart and self._should_restart_for_empty(scan):
            self._restarted_for_empty = True
            try:
                devices = self._auto_restart_and_scan()
            except AdbError as exc:
                return Diagnosis(
                    ConnectionState.ADB_ERROR,
                    detail=(
                        "Đã thử khởi động lại ADB nhưng không thành công. "
                        + str(exc)
                    ),
                )
            auto_restarted = True
            devices = self._settle(devices)
            if devices:
                self._restarted_for_empty = False
                return self._classify(devices)

        detail = ""
        if auto_restarted:
            detail = (
                "Đã tự động chạy lại ADB "
                "(kill-server, start-server, devices) "
                "nhưng vẫn chưa tìm thấy thiết bị."
            )

        return Diagnosis(
            ConnectionState.NO_DEVICE,
            detail=detail,
            adb_interface=scan.adb_interface,
        )