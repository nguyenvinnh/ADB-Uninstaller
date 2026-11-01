from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class DeviceInfo:
    serial: str
    state: str
    model: str = ""
    name: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.model or self.serial


@dataclass(frozen=True)
class AppInfo:
    package: str
    label: str


class ConnectionState(str, Enum):
    READY = "ready"
    NO_DEVICE = "no_device"
    NO_DRIVER = "no_driver"
    UNAUTHORIZED = "unauthorized"
    OFFLINE = "offline"
    ADB_ERROR = "adb_error"


@dataclass(frozen=True)
class UsbProblem:
    """Thiết bị USB mà Windows báo lỗi (thường là thiếu driver)."""

    name: str
    device_id: str
    code: int


@dataclass(frozen=True)
class Diagnosis:
    state: ConnectionState
    devices: list[DeviceInfo] = field(default_factory=list)
    detail: str = ""
    problems: tuple[UsbProblem, ...] = ()
    # Driver ADB đã có nhưng ADB chưa thấy máy (thường do chưa bật Gỡ lỗi USB).
    adb_interface: bool = False


@dataclass(frozen=True)
class ConnectionResult:
    diagnosis: Diagnosis


@dataclass(frozen=True)
class DeviceResult:
    devices: list[DeviceInfo]


@dataclass(frozen=True)
class PackageResult:
    serial: str
    request_id: int
    apps: list[AppInfo]
    # package -> đường dẫn APK hiện tại trên máy.
    apk_paths: dict[str, str] = field(default_factory=dict)
    # package -> tên đã có trong cache (khớp đường dẫn APK hiện tại).
    labels: dict[str, str] = field(default_factory=dict)
    # Các package có biểu tượng mở được; None nếu máy không cho biết.
    launchable: frozenset[str] | None = None


@dataclass(frozen=True)
class MetadataResult:
    serial: str
    app: AppInfo


@dataclass(frozen=True)
class MetadataFailure:
    serial: str
    package: str
    message: str


@dataclass(frozen=True)
class BatchUninstallProgress:
    completed: int
    total: int
    package: str
    attempt: int = 1
    max_attempts: int = 1
    verifying: bool = False


@dataclass(frozen=True)
class BatchUninstallResult:
    serial: str
    successful: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]
    attempts: int = 1


@dataclass(frozen=True)
class ErrorResult:
    message: str
    serial: str | None = None
    request_id: int | None = None


UiEvent = (
    ConnectionResult
    | DeviceResult
    | PackageResult
    | MetadataResult
    | MetadataFailure
    | BatchUninstallProgress
    | BatchUninstallResult
    | ErrorResult
)
