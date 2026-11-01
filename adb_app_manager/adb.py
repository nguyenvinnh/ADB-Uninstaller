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


def parse_devices(output: str) -> list[DeviceInfo]:
    devices: list[DeviceInfo] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] != "List" and not parts[0].startswith("*"):
            model = ""
            for part in parts[2:]:
                if part.startswith("model:"):
                    model = part[len("model:") :].replace("_", " ")
                    break
            devices.append(DeviceInfo(parts[0], parts[1], model))
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
        """Tắt rồi bật lại ADB server.

        Việc này khiến điện thoại hiện lại hộp thoại "Cho phép gỡ lỗi USB?"
        nếu máy tính chưa được cấp quyền.
        """
        try:
            self.run(["kill-server"], timeout=15)
        except AdbError:
            pass
        self.run(["start-server"], timeout=30)

    def device_name(self, serial: str) -> str:
        """Tên thân thiện của máy, ví dụ "Samsung Galaxy S21"."""

        def prop(name: str) -> str:
            try:
                return self.run(
                    ["shell", "getprop", name], device=serial, timeout=10
                ).strip()
            except AdbError:
                return ""

        manufacturer = prop("ro.product.manufacturer")
        market = prop("ro.product.marketname") or prop("ro.config.marketing_name")
        name = market or prop("ro.product.model")
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
