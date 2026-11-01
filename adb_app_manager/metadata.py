from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from pathlib import Path

from .adb import AdbClient
from .models import AppInfo


class MetadataError(Exception):
    pass


def find_aapt() -> str | None:
    path_aapt = shutil.which("aapt")
    if path_aapt:
        return path_aapt

    sdk_roots = {
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
        str(Path.home() / "AppData" / "Local" / "Android" / "Sdk"),
    }
    candidates = [
        path
        for root in sdk_roots
        if root
        for path in (Path(root) / "build-tools").glob("*/aapt.exe")
    ]
    if not candidates:
        return None
    return str(
        max(
            candidates,
            key=lambda path: tuple(
                int(part) for part in re.findall(r"\d+", path.parent.name)
            ),
        )
    )


def parse_apk_label(output: str) -> str | None:
    label_match = re.search(r"^application-label:'(.*)'$", output, re.MULTILINE)
    label = label_match.group(1).strip() if label_match else None
    return label or None


def _cache_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / ".local"
    return base / "AdbAppManager" / "metadata.sqlite3"


class MetadataReader:
    def __init__(self, adb: AdbClient) -> None:
        self.adb = adb
        self.aapt = find_aapt()
        self.cache_path = _cache_path()
        self._database: sqlite3.Connection | None = None
        # Cache được dùng từ nhiều luồng (tải danh sách và luồng đọc tên).
        self._lock = threading.Lock()

    def is_available(self) -> bool:
        """Có aapt để đọc tên hay không (thử tìm lại nếu trước đó chưa có)."""
        if self.aapt is None:
            self.aapt = find_aapt()
        return self.aapt is not None

    def close(self) -> None:
        with self._lock:
            if self._database is not None:
                self._database.close()
                self._database = None

    def _get_database(self) -> sqlite3.Connection:
        # Chỉ gọi khi đang giữ self._lock.
        if self._database is None:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._database = sqlite3.connect(
                self.cache_path, check_same_thread=False
            )
            self._database.execute(
                "CREATE TABLE IF NOT EXISTS app_metadata ("
                "serial TEXT NOT NULL, package TEXT NOT NULL, apk_path TEXT NOT NULL, "
                "label TEXT NOT NULL, "
                "PRIMARY KEY (serial, package, apk_path))"
            )
            self._database.commit()
        return self._database

    def cached_labels(
        self, serial: str, apk_paths: dict[str, str]
    ) -> dict[str, str]:
        """Đọc toàn bộ tên đã lưu của một thiết bị bằng một truy vấn.

        Chỉ giữ các mục có đường dẫn APK trùng với đường dẫn hiện tại, tức là
        ứng dụng chưa được cập nhật kể từ lần đọc trước.
        """
        if not apk_paths:
            return {}
        try:
            with self._lock:
                rows = self._get_database().execute(
                    "SELECT package, apk_path, label FROM app_metadata "
                    "WHERE serial = ?",
                    (serial,),
                ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            raise MetadataError(f"Không thể đọc cache metadata: {exc}") from exc
        return {
            package: label
            for package, path, label in rows
            if apk_paths.get(package) == path
        }

    def _cached(self, serial: str, package: str, apk_path: str) -> AppInfo | None:
        try:
            with self._lock:
                row = self._get_database().execute(
                    "SELECT label FROM app_metadata "
                    "WHERE serial = ? AND package = ? AND apk_path = ?",
                    (serial, package, apk_path),
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise MetadataError(f"Không thể đọc cache metadata: {exc}") from exc
        if row is None:
            return None
        return AppInfo(package, row[0])

    def _store(self, serial: str, apk_path: str, app: AppInfo) -> None:
        try:
            with self._lock:
                database = self._get_database()
                database.execute(
                    "DELETE FROM app_metadata WHERE serial = ? AND package = ? "
                    "AND apk_path != ?",
                    (serial, app.package, apk_path),
                )
                database.execute(
                    "INSERT OR REPLACE INTO app_metadata "
                    "(serial, package, apk_path, label) VALUES (?, ?, ?, ?)",
                    (serial, app.package, apk_path, app.label),
                )
                database.commit()
        except (OSError, sqlite3.Error) as exc:
            raise MetadataError(f"Không thể lưu cache metadata: {exc}") from exc

    def read_app(self, serial: str, package: str, apk_path: str) -> AppInfo:
        cached = self._cached(serial, package, apk_path)
        if cached is not None:
            return cached
        if not self.is_available():
            raise MetadataError(
                "Không tìm thấy aapt; hãy cài Android SDK Build-Tools để đọc tên ứng dụng."
            )

        with tempfile.TemporaryDirectory(prefix="adb-app-manager-") as temp_dir:
            local_apk = Path(temp_dir) / "app.apk"
            self.adb.pull(serial, apk_path, local_apk)
            try:
                result = subprocess.run(
                    [self.aapt, "dump", "badging", str(local_apk)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired as exc:
                raise MetadataError("aapt không phản hồi khi đọc APK.") from exc
            except OSError as exc:
                raise MetadataError(f"Không thể chạy aapt: {exc}") from exc

            if result.returncode != 0:
                raise MetadataError(
                    result.stderr.strip() or f"aapt trả về mã lỗi {result.returncode}."
                )
            label = parse_apk_label(result.stdout)

        app = AppInfo(package, label or package)
        self._store(serial, apk_path, app)
        return app
