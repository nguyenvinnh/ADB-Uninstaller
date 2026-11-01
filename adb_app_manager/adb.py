from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from .models import DeviceInfo


class AdbError(Exception):
    pass


_LAUNCHER_LINE = re.compile(r"^([A-Za-z0-9_.]+)/\S+$")
_PROP_LINE = re.compile(r"^\[(.+?)\]: \[(.*)\]$")

# Các trạng thái mà `adb devices` có thể trả về.
_KNOWN_STATES = {
    "device",
    "offline",
    "unauthorized",
    "authorizing",
    "connecting",
    "recovery",
    "sideload",
    "bootloader",
    "rescue",
    "host",
    "no_permissions",
}
_META_PREFIXES = ("model:", "product:", "device:", "transport_id:", "usb:")


def parse_devices(output: str) -> list[DeviceInfo]:
    devices: list[DeviceInfo] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("*") or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue

        serial = parts[0]
        if line[len(serial) :].lstrip().startswith("no permissions"):
            state = "no_permissions"
            extras = parts[3:]
        else:
            state = parts[1]
            extras = parts[2:]

        has_meta = any(part.startswith(_META_PREFIXES) for part in extras)
        # Bỏ qua các dòng không phải thiết bị, ví dụ cảnh báo lệch phiên bản
        # "adb server version (41) doesn't match this client (40)...".
        if state not in _KNOWN_STATES and not has_meta:
            continue

        model = ""
        for part in extras:
            if part.startswith("model:"):
                model = part[len("model:") :].replace("_", " ")
                break
        devices.append(DeviceInfo(serial, state, model))
    return devices


def parse_packages(output: str) -> list[str]:
    packages = {
        line[len("package:") :].strip()
        for line in output.splitlines()
        if line.startswith("package:") and line[len("package:") :].strip()
    }
    return sorted(packages, key=str.casefold)


def parse_apk_paths(output: str) -> dict[str, str]:
    apk_paths: dict[str, str] = {}
    for line in output.splitlines():
        if not line.startswith("package:"):
            continue
        value = line[len("package:") :]
        if "=" not in value:
            continue
        path, package = value.rsplit("=", 1)
        path = path.strip()
        package = package.strip()
        if package and path.endswith(".apk"):
            if package not in apk_paths or Path(path).name == "base.apk":
                apk_paths[package] = path
    return apk_paths


def parse_launcher_packages(output: str) -> set[str]:
    """Lấy package từ kết quả `query-activities --brief` (dòng `pkg/Activity`)."""
    packages: set[str] = set()
    for line in output.splitlines():
        match = _LAUNCHER_LINE.match(line.strip())
        if match:
            packages.add(match.group(1))
    return packages


def parse_props(output: str) -> dict[str, str]:
    """Đọc kết quả `getprop` (dòng `[key]: [value]`)."""
    props: dict[str, str] = {}
    for line in output.splitlines():
        match = _PROP_LINE.match(line.strip())
        if match:
            props[match.group(1)] = match.group(2).strip()
    return props


def _is_success(output: str) -> bool:
    return any(line.strip() == "Success" for line in output.splitlines())


class AdbClient:
    def __init__(self, executable: str) -> None:
        self.executable = executable

    def run(
        self,
        arguments: list[str],
        device: str | None = None,
        timeout: int = 30,
    ) -> str:
        command = [self.executable]
        if device:
            command.extend(["-s", device])
        command.extend(arguments)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB không phản hồi sau {timeout} giây.") from exc
        except OSError as exc:
            raise AdbError(f"Không thể chạy ADB: {exc}") from exc

        output = result.stdout.strip()
        error = result.stderr.strip()
        if result.returncode != 0:
            raise AdbError(error or output or f"ADB trả về mã lỗi {result.returncode}.")
        if "Failure [" in output or "Failure [" in error:
            raise AdbError(error or output)
        return output

    def restart_server(self) -> None:
        """Thử khởi động lại ADB server.

        Lỗi từ kill-server không ngăn việc thử start-server, vì server có thể
        vốn đã dừng. Nếu start-server thất bại, thông tin lỗi được giữ lại
        để ConnectionChecker có thể hiển thị cho người dùng.
        """
        kill_error = ""

        try:
            self.run(["kill-server"], timeout=15)
        except AdbError as exc:
            kill_error = str(exc)

        try:
            self.run(["start-server"], timeout=30)
        except AdbError as exc:
            if kill_error:
                raise AdbError(
                    f"Không thể dừng ADB server: {kill_error}\n"
                    f"Không thể khởi động ADB server: {exc}"
                ) from exc
            raise

    def device_name(self, serial: str) -> str:
        """Tên thân thiện của máy, ví dụ "Samsung Galaxy S21".

        Chỉ dùng một lệnh `getprop` thay vì nhiều lệnh riêng lẻ.
        """
        try:
            output = self.run(["shell", "getprop"], device=serial, timeout=10)
        except AdbError:
            return ""
        props = parse_props(output)

        manufacturer = props.get("ro.product.manufacturer", "")
        market = props.get("ro.product.marketname") or props.get(
            "ro.config.marketing_name", ""
        )
        name = market or props.get("ro.product.model", "")
        if (
            name
            and manufacturer
            and not name.lower().startswith(manufacturer.lower())
        ):
            name = f"{manufacturer.title()} {name}"
        return name.strip()

    def list_devices(self) -> list[DeviceInfo]:
        devices = parse_devices(self.run(["devices", "-l"], timeout=15))
        result: list[DeviceInfo] = []
        for device in devices:
            if device.state == "device":
                name = self.device_name(device.serial) or device.model
                device = replace(device, name=name)
            result.append(device)
        return result

    def ready_serials(self) -> set[str]:
        """Kiểm tra nhanh các máy đang sẵn sàng (không đọc tên máy)."""
        output = self.run(["devices"], timeout=10)
        return {d.serial for d in parse_devices(output) if d.state == "device"}

    def list_packages(self, serial: str, filters: list[str]) -> list[str]:
        output = self.run(
            ["shell", "pm", "list", "packages", *filters],
            device=serial,
            timeout=30,
        )
        packages = parse_packages(output)
        if not packages and output:
            raise AdbError(
                "ADB không trả về dòng package hợp lệ: " + output[:500]
            )
        return packages

    def installed_packages(self, serial: str) -> set[str]:
        """Danh sách package hiện còn trên máy (cho người dùng hiện tại).

        Dùng để kiểm tra lại sau khi gỡ. Nếu ADB không trả về package nào,
        coi như không kiểm tra được để tránh báo thành công giả.
        """
        output = self.run(
            ["shell", "pm", "list", "packages"],
            device=serial,
            timeout=30,
        )
        packages = parse_packages(output)
        if not packages:
            raise AdbError("ADB không trả về danh sách package để kiểm tra lại.")
        return set(packages)

    def list_apk_paths(self, serial: str) -> dict[str, str]:
        output = self.run(
            ["shell", "pm", "list", "packages", "-f"],
            device=serial,
            timeout=45,
        )
        return parse_apk_paths(output)

    def list_launchable_packages(self, serial: str) -> frozenset[str] | None:
        """Các package có biểu tượng mở được (có launcher activity).

        Chỉ những ứng dụng này mới cần đọc tên; hàng trăm gói nền/dịch vụ
        hệ thống không có giao diện nên bỏ qua để tiết kiệm thời gian.
        Trả về None nếu máy không hỗ trợ lệnh để không bỏ sót ứng dụng nào.
        """
        try:
            output = self.run(
                [
                    "shell",
                    "cmd",
                    "package",
                    "query-activities",
                    "--brief",
                    "-a",
                    "android.intent.action.MAIN",
                    "-c",
                    "android.intent.category.LAUNCHER",
                ],
                device=serial,
                timeout=30,
            )
        except AdbError:
            return None
        packages = parse_launcher_packages(output)
        return frozenset(packages) if packages else None

    def pull(self, serial: str, remote_path: str, local_path: Path) -> None:
        self.run(
            ["pull", remote_path, str(local_path)],
            device=serial,
            timeout=60,
        )

    def uninstall(self, serial: str, package: str) -> None:
        """Gỡ ứng dụng.

        Thử gỡ bình thường trước. Nếu thất bại (thường gặp với ứng dụng
        hệ thống như YouTube), gỡ cho người dùng hiện tại (user 0) bằng
        `pm uninstall -k --user 0`. Cách này không cần root và có thể khôi
        phục bằng `cmd package install-existing <package>`.

        Lưu ý: lệnh trả về thành công chưa chắc ứng dụng đã biến mất (ví dụ
        chỉ gỡ bản cập nhật). Nơi gọi cần kiểm tra lại bằng
        `installed_packages`.
        """
        normal_error = ""
        try:
            output = self.run(["uninstall", package], device=serial, timeout=60)
            if _is_success(output):
                return
            normal_error = output
        except AdbError as exc:
            normal_error = str(exc)

        try:
            output = self.run(
                ["shell", "pm", "uninstall", "-k", "--user", "0", package],
                device=serial,
                timeout=60,
            )
        except AdbError as exc:
            raise AdbError(
                f"Gỡ thông thường lỗi: {normal_error or 'không rõ'}; "
                f"gỡ cho user 0 lỗi: {exc}"
            ) from exc

        if not _is_success(output):
            raise AdbError(
                output
                or normal_error
                or "ADB không xác nhận gỡ ứng dụng thành công."
            )


def find_adb() -> str:
    folder = Path(__file__).resolve().parent.parent
    for name in ("adb.exe", "adb"):
        candidate = folder / name
        if candidate.is_file():
            return str(candidate)
    path_adb = shutil.which("adb")
    if path_adb:
        return path_adb
    raise FileNotFoundError(
        "Không tìm thấy adb.exe trong thư mục chương trình hoặc trong PATH."
    )
