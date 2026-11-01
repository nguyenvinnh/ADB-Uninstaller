from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import webbrowser
from collections.abc import Callable
from tkinter import messagebox, ttk

from .adb import AdbClient, AdbError
from .app_list import AppList, fold_text
from .connection import ConnectionChecker, describe_problem, vendor_id, vendor_name
from .metadata import MetadataError, MetadataReader
from .models import (
    AppInfo,
    BatchUninstallProgress,
    BatchUninstallResult,
    ConnectionResult,
    ConnectionState,
    DeviceInfo,
    Diagnosis,
    ErrorResult,
    MetadataFailure,
    MetadataResult,
    PackageResult,
    UiEvent,
)

BG = "#f4f6fa"
CARD = "#ffffff"
TEXT = "#1f2937"
MUTED = "#6b7280"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
DANGER = "#dc2626"
DANGER_DARK = "#b91c1c"
BORDER = "#d9dee7"

MAX_LOG_LINES = 2000
SEARCH_DEBOUNCE_MS = 500
GOOGLE_USB_DRIVER_URL = "https://developer.android.com/studio/run/win-usb"

GateButton = tuple[str, Callable[[], None], bool]


class AppManager:
    FILTERS = {
        "Tất cả ứng dụng": [],
        "Ứng dụng người dùng": ["-3"],
        "Ứng dụng hệ thống": ["-s"],
    }
    UNINSTALL_ATTEMPTS = 3
    VERIFY_DELAY = 1.0
    PRIORITY_VISIBLE = 0
    PRIORITY_BACKGROUND = 1
    GATE_POLL_MS = 3000

    def __init__(self, root: tk.Tk, adb: AdbClient) -> None:
        self.root = root
        self.adb = adb
        self.checker = ConnectionChecker(adb)
        self.metadata_reader = MetadataReader(adb)
        self.events: queue.Queue[UiEvent] = queue.Queue()
        # (ưu tiên, số thứ tự, (serial, package), token) — ưu tiên nhỏ chạy trước.
        self.metadata_queue: queue.PriorityQueue[
            tuple[int, int, tuple[str, str] | None, int]
        ] = queue.PriorityQueue()
        self.metadata_seq = 0
        self.metadata_thread = threading.Thread(
            target=self._metadata_loop,
            name="app-metadata-loader",
            daemon=True,
        )
        self.metadata_thread.start()

        self.devices: dict[str, DeviceInfo] = {}
        self.apps: dict[str, AppInfo] = {}
        self.packages: list[str] = []
        self.apk_paths: dict[str, str] = {}
        self.launchable: frozenset[str] | None = None
        self.metadata_available = self.metadata_reader.aapt is not None
        self.metadata_high: set[tuple[str, str]] = set()
        self.metadata_queued_bg: set[tuple[str, str]] = set()
        self.metadata_attempted: set[tuple[str, str]] = set()
        self.metadata_failures: set[str] = set()
        self.search_needed: set[tuple[str, str]] = set()
        self.bg_token = 0
        self.selected_packages: set[str] = set()
        self.current_serial: str | None = None
        self.request_id = 0
        self.device_loading = False
        self.uninstalling = False
        self.visible_job = 0
        self.render_job = 0
        self.search_job = 0
        self.last_metadata_error = ""
        self.pending_status = ""
        self.preserve_scroll = False

        # Màn hình kiểm tra kết nối (driver / cấp quyền).
        self.gate_visible = False
        self.gate_job = 0
        self.auth_restart_done = False
        self.last_logged_state: tuple[ConnectionState, str] | None = None

        self.log_lines: list[str] = []
        self.log_window: tk.Toplevel | None = None
        self.log_text: tk.Text | None = None

        self.device_label_var = tk.StringVar(value="Đang tìm thiết bị...")
        self.filter_var = tk.StringVar(value="Ứng dụng người dùng")
        self.search_var = tk.StringVar()
        self.select_visible_var = tk.BooleanVar()
        self.status_var = tk.StringVar(value="Đang kiểm tra kết nối...")
        self.count_var = tk.StringVar(value="0 ứng dụng")
        self.selected_count_var = tk.StringVar(value="Đã chọn 0")
        self.gate_title_var = tk.StringVar()
        self.gate_msg_var = tk.StringVar()
        self.gate_steps_var = tk.StringVar()
        self.gate_hint_var = tk.StringVar()

        self._build_ui()
        self.search_var.trace_add("write", self._on_search_changed)
        self.root.after(100, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.refresh_devices()

    # ------------------------------------------------------------------ UI
    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.configure(background=BG)

        style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Title.TLabel", font=("Segoe UI", 17, "bold"))
        style.configure(
            "Device.TLabel", foreground=ACCENT, font=("Segoe UI", 10, "bold")
        )
        style.configure("Muted.TLabel", foreground=MUTED, font=("Segoe UI", 9))
        style.configure(
            "Badge.TLabel",
            background="#e8f0fe",
            foreground=ACCENT,
            font=("Segoe UI", 9, "bold"),
            padding=(10, 4),
        )

        # Thẻ hướng dẫn kết nối
        style.configure(
            "Card.TFrame",
            background=CARD,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            relief="solid",
            borderwidth=1,
        )
        style.configure("CardInner.TFrame", background=CARD)
        style.configure(
            "CardTitle.TLabel",
            background=CARD,
            foreground=TEXT,
            font=("Segoe UI", 15, "bold"),
        )
        style.configure(
            "CardText.TLabel",
            background=CARD,
            foreground=TEXT,
            font=("Segoe UI", 10),
        )
        style.configure(
            "CardMuted.TLabel",
            background=CARD,
            foreground=MUTED,
            font=("Segoe UI", 9),
        )

        style.configure(
            "TButton",
            padding=(14, 7),
            background=CARD,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=CARD,
            darkcolor=CARD,
            focuscolor=CARD,
            focusthickness=0,
            relief="flat",
        )
        style.map(
            "TButton",
            background=[("disabled", "#f1f3f6"), ("active", "#eef2f7")],
            foreground=[("disabled", "#9aa3af")],
        )
        style.configure(
            "Small.TButton", padding=(10, 3), font=("Segoe UI", 9)
        )
        style.configure(
            "Primary.TButton",
            background=ACCENT,
            foreground="#ffffff",
            bordercolor=ACCENT,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("disabled", "#a9c1f3"), ("active", ACCENT_DARK)],
            foreground=[("disabled", "#ffffff")],
            bordercolor=[("disabled", "#a9c1f3")],
        )
        style.configure(
            "Danger.TButton",
            background=DANGER,
            foreground="#ffffff",
            bordercolor=DANGER,
            lightcolor=DANGER,
            darkcolor=DANGER,
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "Danger.TButton",
            background=[("disabled", "#f1b5b5"), ("active", DANGER_DARK)],
            foreground=[("disabled", "#ffffff")],
            bordercolor=[("disabled", "#f1b5b5")],
        )

        style.configure(
            "TEntry",
            padding=7,
            fieldbackground=CARD,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", ACCENT)],
            lightcolor=[("focus", ACCENT)],
            darkcolor=[("focus", ACCENT)],
            fieldbackground=[("disabled", "#f1f3f6")],
        )
        style.configure(
            "TCombobox",
            padding=6,
            fieldbackground=CARD,
            background=CARD,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            arrowcolor=MUTED,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", CARD), ("disabled", "#f1f3f6")],
            foreground=[("disabled", "#9aa3af")],
            bordercolor=[("focus", ACCENT)],
        )
        style.configure("TCheckbutton", background=BG, foreground=TEXT)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor="#e5e9f0",
            background=ACCENT,
            bordercolor=BG,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            thickness=5,
        )

    def _build_ui(self) -> None:
        self.root.title("Trình quản lý ứng dụng Android qua ADB")
        self.root.geometry("860x660")
        self.root.minsize(640, 440)
        self._configure_style()

        frame = ttk.Frame(self.root, padding=(20, 16, 20, 12))
        frame.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        # Hàng 0: tiêu đề + tên máy + làm mới + loại ứng dụng
        header = ttk.Frame(frame)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        title_box = ttk.Frame(header)
        title_box.grid(row=0, column=0, sticky="w")
        ttk.Label(
            title_box, text="Quản lý ứng dụng Android", style="Title.TLabel"
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            title_box, textvariable=self.device_label_var, style="Device.TLabel"
        ).grid(row=1, column=0, sticky="w", pady=(1, 0))

        self.switch_button = ttk.Button(
            header,
            text="Đổi thiết bị",
            style="Small.TButton",
            command=self._next_device,
        )
        self.switch_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.switch_button.grid_remove()
        self.refresh_button = ttk.Button(
            header, text="↻  Làm mới", command=self.refresh_devices
        )
        self.refresh_button.grid(row=0, column=2, sticky="e", padx=(8, 8))
        self.filter_combo = ttk.Combobox(
            header,
            textvariable=self.filter_var,
            values=list(self.FILTERS),
            state="readonly",
            width=20,
        )
        self.filter_combo.grid(row=0, column=3, sticky="e")
        self.filter_combo.bind("<<ComboboxSelected>>", self._on_filter_selected)

        # Hàng 1: tìm kiếm + danh sách
        self.list_panel = ttk.Frame(frame)
        self.list_panel.grid(row=1, column=0, sticky="nsew")
        self.list_panel.columnconfigure(0, weight=1)
        self.list_panel.rowconfigure(1, weight=1)

        search_bar = ttk.Frame(self.list_panel)
        search_bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        search_bar.columnconfigure(0, weight=1)
        self.search_entry = ttk.Entry(search_bar, textvariable=self.search_var)
        self.search_entry.grid(row=0, column=0, sticky="ew")
        ttk.Label(search_bar, textvariable=self.count_var, style="Muted.TLabel").grid(
            row=0, column=1, sticky="e", padx=(12, 0)
        )

        self.app_list = AppList(
            self.list_panel,
            on_selection_changed=self._on_selection_changed,
            on_visible_changed=self._on_visible_apps_changed,
        )
        self.app_list.grid(row=1, column=0, sticky="nsew")

        # Hàng 2: thao tác
        self.actions = ttk.Frame(frame)
        self.actions.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        self.actions.columnconfigure(1, weight=1)
        self.select_visible = ttk.Checkbutton(
            self.actions,
            text="Chọn tất cả đang hiển thị",
            variable=self.select_visible_var,
            command=self._toggle_visible_selection,
        )
        self.select_visible.grid(row=0, column=0, sticky="w")
        ttk.Label(
            self.actions, textvariable=self.selected_count_var, style="Badge.TLabel"
        ).grid(row=0, column=1, sticky="e", padx=(8, 10))
        self.clear_selection_button = ttk.Button(
            self.actions, text="Bỏ chọn", command=self._clear_selection
        )
        self.clear_selection_button.grid(row=0, column=2, sticky="e", padx=(0, 8))
        self.uninstall_button = ttk.Button(
            self.actions,
            text="Gỡ đã chọn",
            command=self.uninstall_selected,
            style="Danger.TButton",
        )
        self.uninstall_button.grid(row=0, column=3, sticky="e")

        # Màn hình kiểm tra kết nối: thay thế danh sách khi chưa sẵn sàng.
        self.gate = ttk.Frame(frame, style="Card.TFrame", padding=(28, 24))
        self.gate.grid(row=1, column=0, rowspan=2, sticky="nsew")
        self.gate.columnconfigure(0, weight=1)
        ttk.Label(
            self.gate, textvariable=self.gate_title_var, style="CardTitle.TLabel"
        ).grid(row=0, column=0, sticky="w")
        self.gate_msg_label = ttk.Label(
            self.gate,
            textvariable=self.gate_msg_var,
            style="CardText.TLabel",
            justify="left",
            wraplength=560,
        )
        self.gate_msg_label.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.gate_steps_label = ttk.Label(
            self.gate,
            textvariable=self.gate_steps_var,
            style="CardText.TLabel",
            justify="left",
            wraplength=560,
        )
        self.gate_steps_label.grid(row=2, column=0, sticky="w", pady=(14, 0))
        self.gate_buttons = ttk.Frame(self.gate, style="CardInner.TFrame")
        self.gate_buttons.grid(row=3, column=0, sticky="w", pady=(18, 0))
        self.gate_progress = ttk.Progressbar(self.gate, mode="indeterminate")
        self.gate_progress.grid(row=4, column=0, sticky="ew", pady=(20, 0))
        ttk.Label(
            self.gate, textvariable=self.gate_hint_var, style="CardMuted.TLabel"
        ).grid(row=5, column=0, sticky="w", pady=(6, 0))
        self.gate.bind("<Configure>", self._on_gate_resize)

        # Hàng 3: trạng thái ngắn + nút mở nhật ký
        status_bar = ttk.Frame(frame)
        status_bar.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        status_bar.columnconfigure(0, weight=1)
        ttk.Label(
            status_bar, textvariable=self.status_var, style="Muted.TLabel"
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(
            status_bar,
            text="Nhật ký",
            style="Small.TButton",
            command=self._open_log,
        ).grid(row=0, column=1, sticky="e", padx=(8, 0))

        self.progress = ttk.Progressbar(frame, mode="determinate")
        self.progress.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        self.progress.grid_remove()

        # Bắt đầu bằng màn hình "đang kiểm tra kết nối".
        self.gate_visible = False
        self._render_gate_checking()
        self._update_controls()

    # ---------------------------------------------------- màn hình kết nối
    def _on_gate_resize(self, event: tk.Event) -> None:
        wrap = max(event.width - 80, 240)
        self.gate_msg_label.configure(wraplength=wrap)
        self.gate_steps_label.configure(wraplength=wrap)

    def _set_gate_visible(self, visible: bool) -> None:
        if visible == self.gate_visible:
            return
        self.gate_visible = visible
        if visible:
            self.list_panel.grid_remove()
            self.actions.grid_remove()
            self.gate.grid()
            self.gate_progress.start(15)
        else:
            self.gate.grid_remove()
            self.list_panel.grid()
            self.actions.grid()
            self.gate_progress.stop()

    def _set_gate_buttons(self, buttons: list[GateButton]) -> None:
        for child in self.gate_buttons.winfo_children():
            child.destroy()
        for index, (text, command, primary) in enumerate(buttons):
            ttk.Button(
                self.gate_buttons,
                text=text,
                command=command,
                style="Primary.TButton" if primary else "TButton",
            ).grid(row=index // 3, column=index % 3, padx=(0, 8), pady=(0, 8), sticky="w")

    def _render_gate_checking(self) -> None:
        self.gate_title_var.set("Đang kiểm tra kết nối")
        self.gate_msg_var.set("Đang tìm điện thoại Android qua ADB...")
        self.gate_steps_var.set("")
        self.gate_hint_var.set("")
        self._set_gate_buttons([])
        self._set_gate_visible(True)

    def _render_gate(self, diag: Diagnosis) -> None:
        title, message, steps, buttons = self._gate_content(diag)
        self.gate_title_var.set(title)
        self.gate_msg_var.set(message)
        self.gate_steps_var.set("\n".join(steps))
        self.gate_hint_var.set(
            f"Tự động kiểm tra lại mỗi {self.GATE_POLL_MS // 1000} giây."
        )
        self._set_gate_buttons(buttons)
        self._set_gate_visible(True)
        self.status_var.set(title)

    def _vendor_search_url(self, diag: Diagnosis) -> str:
        query = "Android USB driver Windows"
        for problem in diag.problems:
            vendor = vendor_name(problem.device_id)
            vid = vendor_id(problem.device_id)
            if vendor:
                query = f"{vendor} USB driver Windows"
                break
            if vid:
                query = f"VID_{vid} Android USB driver"
                break
        return "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)

    def _gate_content(
        self, diag: Diagnosis
    ) -> tuple[str, str, list[str], list[GateButton]]:
        recheck: GateButton = ("Kiểm tra lại", self._gate_recheck, False)
        state = diag.state

        if state is ConnectionState.UNAUTHORIZED:
            return (
                "Cần cấp quyền gỡ lỗi USB",
                "Điện thoại đã kết nối nhưng chưa cho phép máy tính này truy cập.",
                [
                    "1. Mở khóa màn hình điện thoại.",
                    "2. Khi hộp thoại “Cho phép gỡ lỗi USB?” hiện ra, tích "
                    "“Luôn cho phép từ máy tính này” rồi chạm Cho phép.",
                    "3. Nếu không thấy hộp thoại, bấm “Hiện lại hộp thoại cấp quyền”.",
                    "4. Vẫn không thấy: vào Tùy chọn nhà phát triển > Thu hồi quyền "
                    "gỡ lỗi USB, rồi rút và cắm lại cáp.",
                ],
                [
                    (
                        "Hiện lại hộp thoại cấp quyền",
                        self._gate_restart_adb,
                        True,
                    ),
                    recheck,
                ],
            )

        if state is ConnectionState.OFFLINE:
            return (
                "Thiết bị đang ngoại tuyến",
                f"ADB thấy thiết bị nhưng chưa dùng được: {diag.detail}",
                [
                    "1. Rút cáp, mở khóa điện thoại rồi cắm lại.",
                    "2. Tắt rồi bật lại Gỡ lỗi USB trong Tùy chọn nhà phát triển.",
                    "3. Bấm “Khởi động lại ADB”.",
                ],
                [("Khởi động lại ADB", self._gate_restart_adb, True), recheck],
            )

        if state is ConnectionState.ADB_ERROR:
            return (
                "ADB không chạy được",
                diag.detail[:400] or "Không thể chạy ADB.",
                [
                    "1. Đóng các chương trình đang dùng ADB (Android Studio, "
                    "phần mềm quản lý điện thoại, scrcpy...).",
                    "2. Bấm “Khởi động lại ADB”.",
                    "3. Nếu vẫn lỗi, kiểm tra adb.exe nằm cạnh app_manager.py "
                    "hoặc có trong PATH.",
                ],
                [("Khởi động lại ADB", self._gate_restart_adb, True), recheck],
            )

        if state is ConnectionState.NO_DRIVER:
            lines = [f"• {describe_problem(p)}" for p in diag.problems[:5]]
            if len(diag.problems) > 5:
                lines.append(f"• ... và {len(diag.problems) - 5} thiết bị khác")
            search_url = self._vendor_search_url(diag)
            return (
                "Máy tính chưa có driver cho điện thoại",
                "Windows thấy thiết bị USB nhưng driver chưa hoạt động:\n"
                + "\n".join(lines),
                [
                    "1. Tải driver: Google USB Driver cho Pixel/Nexus, máy hãng khác "
                    "dùng driver của hãng (Samsung, Xiaomi, Oppo...). Giải nén file tải về.",
                    "2. Mở Device Manager, chuột phải thiết bị có dấu chấm than vàng, "
                    "chọn Update driver > Browse my computer for drivers, rồi trỏ tới "
                    "thư mục driver vừa giải nén.",
                    "3. Rút cáp rồi cắm lại. Ứng dụng sẽ tự kiểm tra lại.",
                ],
                [
                    (
                        "Tải Google USB Driver",
                        lambda: self._open_url(GOOGLE_USB_DRIVER_URL),
                        True,
                    ),
                    (
                        "Tìm driver của hãng",
                        lambda: self._open_url(search_url),
                        False,
                    ),
                    ("Mở Device Manager", self._open_device_manager, False),
                    recheck,
                ],
            )

        # NO_DEVICE
        message = "Chưa thấy điện thoại Android nào kết nối với máy tính."
        if diag.adb_interface:
            message += (
                "\nDriver ADB đã có nhưng ADB chưa nhận máy, thường do chưa bật "
                "Gỡ lỗi USB."
            )
        steps = [
            "1. Cắm điện thoại bằng cáp USB truyền dữ liệu (không dùng cáp chỉ sạc). "
            "Thử cổng USB khác nếu cần.",
            "2. Mở khóa điện thoại, kéo thanh thông báo, chạm vào thông báo USB "
            "và chọn “Truyền tệp” (File Transfer).",
            "3. Bật Gỡ lỗi USB: Cài đặt > Tùy chọn nhà phát triển > Gỡ lỗi USB. "
            "Chưa thấy menu thì vào Giới thiệu điện thoại và bấm 7 lần vào Số bản dựng.",
        ]
        if not sys.platform.startswith("win"):
            steps.append(
                "4. Trên Linux/macOS, kiểm tra quyền truy cập USB (udev rules) cho ADB."
            )
        return (
            "Chưa thấy điện thoại",
            message,
            steps,
            [
                ("Kiểm tra lại", self._gate_recheck, True),
                ("Khởi động lại ADB", self._gate_restart_adb, False),
            ],
        )

    def _gate_recheck(self) -> None:
        self.refresh_devices()

    def _gate_restart_adb(self) -> None:
        self.refresh_devices(restart_server=True)

    def _open_url(self, url: str) -> None:
        self._log(f"Mở trình duyệt: {url}")
        try:
            webbrowser.open(url)
        except webbrowser.Error as exc:
            messagebox.showerror(
                "Không mở được trình duyệt", f"{exc}\n\n{url}", parent=self.root
            )

    def _open_device_manager(self) -> None:
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            messagebox.showinfo(
                "Device Manager",
                "Device Manager chỉ có trên Windows.",
                parent=self.root,
            )
            return
        try:
            startfile("devmgmt.msc")
        except OSError as exc:
            messagebox.showerror(
                "Không mở được Device Manager", str(exc), parent=self.root
            )

    def _schedule_gate_poll(self) -> None:
        self._cancel_gate_poll()
        self.gate_job = self.root.after(self.GATE_POLL_MS, self._gate_poll)

    def _cancel_gate_poll(self) -> None:
        if self.gate_job:
            self.root.after_cancel(self.gate_job)
            self.gate_job = 0

    def _gate_poll(self) -> None:
        self.gate_job = 0
        if not self.gate_visible or self.uninstalling:
            return
        if self.device_loading:
            self._schedule_gate_poll()
            return
        self.refresh_devices(quiet=True)

    def _lost_device(self) -> None:
        """Xóa dữ liệu của máy đã mất kết nối."""
        if self.current_serial is None and not self.devices:
            return
        self.current_serial = None
        self.devices = {}
        self._reset_device_state()
        self._update_device_label()
        self._log("Mất kết nối với thiết bị.")

    def _log_diagnosis(self, diag: Diagnosis) -> None:
        key = (diag.state, diag.detail)
        if key == self.last_logged_state:
            return
        self.last_logged_state = key
        messages = {
            ConnectionState.NO_DEVICE: "Chưa thấy thiết bị Android.",
            ConnectionState.NO_DRIVER: "Thiết bị USB chưa có driver.",
            ConnectionState.UNAUTHORIZED: "Thiết bị chưa cấp quyền gỡ lỗi USB.",
            ConnectionState.OFFLINE: "Thiết bị đang ngoại tuyến.",
            ConnectionState.ADB_ERROR: "ADB không chạy được.",
        }
        self._log(messages.get(diag.state, str(diag.state)))
        if diag.detail:
            self._log(f"  Chi tiết: {diag.detail}")
        for problem in diag.problems:
            self._log(f"  {describe_problem(problem)} ({problem.device_id})")

    def _handle_diagnosis(self, diag: Diagnosis) -> None:
        if diag.state is ConnectionState.READY:
            self.auth_restart_done = False
            self.last_logged_state = (ConnectionState.READY, "")
            self._cancel_gate_poll()
            self._set_gate_visible(False)
            self._show_devices(diag.devices)
            return

        if diag.state is not ConnectionState.UNAUTHORIZED:
            self.auth_restart_done = False
        self._lost_device()
        self._log_diagnosis(diag)
        self._render_gate(diag)

        if diag.state is ConnectionState.UNAUTHORIZED and not self.auth_restart_done:
            # Khởi động lại ADB một lần để điện thoại hiện lại hộp thoại cấp quyền.
            self.auth_restart_done = True
            self._log("Khởi động lại ADB để hiện hộp thoại cấp quyền trên điện thoại.")
            self.refresh_devices(restart_server=True, quiet=True)
            return
        self._schedule_gate_poll()

    # ----------------------------------------------------------------- log
    def _log(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        self.log_lines.append(line)
        if len(self.log_lines) > MAX_LOG_LINES:
            del self.log_lines[: MAX_LOG_LINES // 4]
        self._append_log_text(line)

    def _append_log_text(self, line: str) -> None:
        text = self.log_text
        if text is None or not text.winfo_exists():
            return
        text.configure(state="normal")
        text.insert(tk.END, line + "\n")
        text.configure(state="disabled")
        text.see(tk.END)

    def _open_log(self) -> None:
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.deiconify()
            self.log_window.lift()
            self.log_window.focus_force()
            return

        window = tk.Toplevel(self.root)
        window.title("Nhật ký")
        window.geometry("720x420")
        window.minsize(420, 240)
        window.configure(background=BG)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)

        text = tk.Text(
            window,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
            background=CARD,
            foreground=TEXT,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            padx=8,
            pady=6,
        )
        scrollbar = ttk.Scrollbar(window, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.grid(row=0, column=0, sticky="nsew", padx=(12, 0), pady=(12, 8))
        scrollbar.grid(row=0, column=1, sticky="ns", pady=(12, 8), padx=(0, 12))

        buttons = ttk.Frame(window)
        buttons.grid(row=1, column=0, columnspan=2, sticky="e", padx=12, pady=(0, 12))
        ttk.Button(
            buttons, text="Xóa nhật ký", style="Small.TButton", command=self._clear_log
        ).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(
            buttons, text="Đóng", style="Small.TButton", command=self._close_log
        ).grid(row=0, column=1)

        self.log_window = window
        self.log_text = text
        window.protocol("WM_DELETE_WINDOW", self._close_log)

        text.configure(state="normal")
        text.insert(tk.END, "\n".join(self.log_lines) + ("\n" if self.log_lines else ""))
        text.configure(state="disabled")
        text.see(tk.END)

    def _clear_log(self) -> None:
        self.log_lines.clear()
        text = self.log_text
        if text is not None and text.winfo_exists():
            text.configure(state="normal")
            text.delete("1.0", tk.END)
            text.configure(state="disabled")

    def _close_log(self) -> None:
        if self.log_window is not None and self.log_window.winfo_exists():
            self.log_window.destroy()
        self.log_window = None
        self.log_text = None

    # ------------------------------------------------------------- devices
    def refresh_devices(
        self, restart_server: bool = False, quiet: bool = False
    ) -> None:
        """Kiểm tra kết nối (ADB, driver, cấp quyền) rồi mới tải danh sách."""
        if self.uninstalling:
            return
        if self.device_loading:
            if not quiet:
                # Đang có lần kiểm tra khác: thử lại ngay khi nó xong.
                self.root.after(
                    400, lambda: self.refresh_devices(restart_server=restart_server)
                )
            return
        self._cancel_gate_poll()
        self.device_loading = True
        if not quiet:
            self.status_var.set("Đang kiểm tra kết nối thiết bị...")
            self._log(
                "Khởi động lại ADB và kiểm tra kết nối."
                if restart_server
                else "Kiểm tra kết nối thiết bị."
            )
            self._update_controls()
        checker = self.checker
        threading.Thread(
            target=self._run_operation,
            args=(
                lambda: ConnectionResult(
                    checker.diagnose(restart=restart_server, fresh=not quiet)
                ),
            ),
            daemon=True,
        ).start()

    def _run_operation(
        self,
        operation: Callable[[], UiEvent],
        serial: str | None = None,
        request_id: int | None = None,
    ) -> None:
        try:
            result = operation()
        except AdbError as exc:
            result = ErrorResult(str(exc), serial, request_id)
        except OSError as exc:
            result = ErrorResult(
                f"Không thể hoàn tất thao tác: {exc}", serial, request_id
            )
        self.events.put(result)

    def _load_packages(self) -> None:
        serial = self.current_serial
        if not serial or serial not in self.devices or self.uninstalling:
            return
        self.request_id += 1
        request_id = self.request_id
        filters = self.FILTERS[self.filter_var.get()].copy()
        known_apps = self.apps.copy()
        self.status_var.set("Đang đọc danh sách ứng dụng...")
        self._update_controls()

        def operation() -> PackageResult:
            packages = self.adb.list_packages(serial, filters)
            try:
                apk_paths = self.adb.list_apk_paths(serial)
            except AdbError:
                apk_paths = {}
            try:
                # Một truy vấn lấy mọi tên đã lưu, không cần tải lại APK.
                labels = self.metadata_reader.cached_labels(serial, apk_paths)
            except MetadataError:
                labels = {}
            launchable = self.adb.list_launchable_packages(serial)

            apps: list[AppInfo] = []
            for package in packages:
                known = known_apps.get(package)
                if known is not None and known.label != package:
                    apps.append(known)
                else:
                    apps.append(AppInfo(package, labels.get(package, package)))
            return PackageResult(
                serial,
                request_id,
                apps,
                apk_paths=apk_paths,
                labels=labels,
                launchable=launchable,
            )

        threading.Thread(
            target=self._run_operation,
            args=(operation, serial, request_id),
            daemon=True,
        ).start()

    # ------------------------------------------------------------ metadata
    def _wants_metadata(self, package: str) -> bool:
        """Chỉ ứng dụng có biểu tượng mở được mới cần đọc tên."""
        return self.launchable is None or package in self.launchable

    def _enqueue_metadata(self, packages: list[str], priority: int) -> int:
        serial = self.current_serial
        if not serial or not self.metadata_available:
            return 0
        seen = (
            self.metadata_high
            if priority == self.PRIORITY_VISIBLE
            else self.metadata_queued_bg
        )
        queued = 0
        for package in packages:
            if not self._wants_metadata(package):
                continue
            key = (serial, package)
            if key in self.metadata_attempted or key in seen:
                continue
            seen.add(key)
            self.metadata_seq += 1
            self.metadata_queue.put((priority, self.metadata_seq, key, self.bg_token))
            queued += 1
        return queued

    def _metadata_loop(self) -> None:
        processed: set[tuple[str, str]] = set()
        try:
            while True:
                priority, _seq, key, token = self.metadata_queue.get()
                if key is None:
                    return
                # Việc nền của từ khóa cũ đã bị hủy.
                if priority == self.PRIORITY_BACKGROUND and token != self.bg_token:
                    continue
                serial, package = key
                if (
                    serial != self.current_serial
                    or key in processed
                    or key in self.metadata_attempted
                ):
                    continue
                processed.add(key)
                try:
                    apk_path = self.apk_paths.get(package)
                    if apk_path is None:
                        raise MetadataError(
                            f"Không tìm thấy đường dẫn APK cho {package}."
                        )
                    try:
                        app = self.metadata_reader.read_app(serial, package, apk_path)
                    except AdbError:
                        # Đường dẫn có thể đã đổi do ứng dụng vừa cập nhật.
                        paths = self.adb.list_apk_paths(serial)
                        if serial == self.current_serial:
                            self.apk_paths = paths
                        refreshed_path = paths.get(package)
                        if refreshed_path is None or refreshed_path == apk_path:
                            raise
                        app = self.metadata_reader.read_app(
                            serial, package, refreshed_path
                        )
                    self.events.put(MetadataResult(serial, app))
                except (AdbError, MetadataError) as exc:
                    self.events.put(MetadataFailure(serial, package, str(exc)))
        finally:
            self.metadata_reader.close()

    # -------------------------------------------------------------- events
    def _drain_events(self) -> None:
        while True:
            try:
                result = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(result)
        self.root.after(100, self._drain_events)

    def _handle_event(self, result: UiEvent) -> None:
        if isinstance(result, ConnectionResult):
            self.device_loading = False
            self._handle_diagnosis(result.diagnosis)
        elif isinstance(result, PackageResult):
            if result.request_id != self.request_id or result.serial != self.current_serial:
                return
            serial = result.serial
            self.packages = [app.package for app in result.apps]
            if result.apk_paths:
                self.apk_paths = result.apk_paths
            self.launchable = result.launchable
            self.metadata_available = self.metadata_reader.is_available()
            for app in result.apps:
                current = self.apps.get(app.package)
                # Không ghi đè tên đã đọc được bằng tên package.
                if current is None or app.label != app.package:
                    self.apps[app.package] = app
            # Tên có sẵn trong cache coi như đã đọc xong, không tải APK lại.
            for package in result.labels:
                self.metadata_attempted.add((serial, package))
            # Không giữ lại các mục đã chọn nhưng không còn trong danh sách.
            self.selected_packages &= set(self.packages)
            keep_scroll = self.preserve_scroll
            self.preserve_scroll = False
            self._render_apps(reset_scroll=not keep_scroll)
            if self.pending_status:
                status = f"{self.pending_status} Còn {len(self.packages)} ứng dụng."
                self.pending_status = ""
            else:
                status = f"Đã tải {len(self.packages)} ứng dụng."
            if not self.metadata_available:
                status += " Chưa có aapt nên chỉ tìm được theo package."
            self.status_var.set(status)
            self._log(
                f"Đã tải {len(self.packages)} ứng dụng "
                f"({len(result.labels)} tên lấy từ cache)."
            )
            if not self.metadata_available:
                self._log(
                    "Không tìm thấy aapt; cài Android SDK Build-Tools để đọc tên ứng dụng."
                )
            # Danh sách đổi thì xác định lại các ứng dụng cần đọc tên.
            self._start_search_metadata()
        elif isinstance(result, MetadataResult):
            if result.serial != self.current_serial:
                return
            self.metadata_attempted.add((result.serial, result.app.package))
            self.metadata_failures.discard(result.app.package)
            self.apps[result.app.package] = result.app
            self._schedule_render()
        elif isinstance(result, MetadataFailure):
            self.metadata_attempted.add((result.serial, result.package))
            if result.serial == self.current_serial:
                self.metadata_failures.add(result.package)
                if result.message != self.last_metadata_error:
                    self._log(f"Không đọc được tên {result.package}: {result.message}")
                self.last_metadata_error = result.message
                self._schedule_render()
        elif isinstance(result, BatchUninstallProgress):
            self.progress.grid()
            if result.verifying:
                self.progress.configure(maximum=max(result.total, 1), value=result.total)
                self.status_var.set(
                    f"Lần {result.attempt}/{result.max_attempts}: đang kiểm tra lại..."
                )
                self._log(
                    f"Lần {result.attempt}/{result.max_attempts}: "
                    "kiểm tra lại ứng dụng còn trên thiết bị."
                )
            else:
                self.progress.configure(
                    maximum=max(result.total, 1), value=result.completed - 1
                )
                self.status_var.set(
                    f"Đang gỡ {result.completed}/{result.total} "
                    f"(lần {result.attempt}/{result.max_attempts})"
                )
                self._log(
                    f"Lần {result.attempt}/{result.max_attempts}: "
                    f"gỡ {result.package} ({result.completed}/{result.total})"
                )
        elif isinstance(result, BatchUninstallResult):
            self.uninstalling = False
            self.progress.grid_remove()
            for package in result.successful:
                self.apps.pop(package, None)
                self.packages = [item for item in self.packages if item != package]
                self.selected_packages.discard(package)
                self.metadata_attempted.discard((result.serial, package))
                self.metadata_high.discard((result.serial, package))
                self.metadata_queued_bg.discard((result.serial, package))
                self.metadata_failures.discard(package)
                self._log(f"Đã xác nhận gỡ: {package}")
            for package, error in result.failed:
                self._log(f"Không gỡ được {package}: {error}")
            self._render_apps(reset_scroll=False)
            # Tải lại danh sách thật từ thiết bị để đối chiếu.
            self.pending_status = self._batch_summary(result)
            self.preserve_scroll = True
            self._load_packages()
            self._update_controls()
            self._show_batch_result(result)
        elif isinstance(result, ErrorResult):
            self.device_loading = False
            self.uninstalling = False
            self.preserve_scroll = False
            self.progress.grid_remove()
            if result.request_id is not None and result.request_id != self.request_id:
                return
            if result.serial is not None and result.serial != self.current_serial:
                return
            self.status_var.set("Lỗi ADB. Bấm Làm mới để kiểm tra lại kết nối.")
            self._log(f"Lỗi ADB: {result.message}")
            messagebox.showerror("Lỗi ADB", result.message, parent=self.root)
        self._update_controls()

    def _schedule_render(self) -> None:
        """Gộp nhiều cập nhật tên liên tiếp thành một lần vẽ lại."""
        if self.render_job:
            return
        self.render_job = self.root.after(200, self._run_scheduled_render)

    def _run_scheduled_render(self) -> None:
        self.render_job = 0
        self._render_apps(reset_scroll=False)
        self._update_metadata_status()

    def _reset_device_state(self) -> None:
        self.apps.clear()
        self.packages.clear()
        self.apk_paths = {}
        self.launchable = None
        self.selected_packages.clear()
        self.metadata_high.clear()
        self.metadata_queued_bg.clear()
        self.metadata_attempted.clear()
        self.metadata_failures.clear()
        self.search_needed = set()
        self.bg_token += 1
        self.app_list.set_selected_packages(set())
        self.app_list.set_apps([])

    def _update_device_label(self) -> None:
        serial = self.current_serial
        if not serial or serial not in self.devices:
            self.device_label_var.set("Chưa kết nối thiết bị")
            self.switch_button.grid_remove()
            return
        text = self.devices[serial].display_name
        if len(self.devices) > 1:
            index = list(self.devices).index(serial) + 1
            text += f"  ·  {index}/{len(self.devices)}"
            self.switch_button.grid()
        else:
            self.switch_button.grid_remove()
        self.device_label_var.set(text)

    def _show_devices(self, all_devices: list[DeviceInfo]) -> None:
        ready = [device for device in all_devices if device.state == "device"]
        self.devices = {device.serial: device for device in ready}
        serials = list(self.devices)

        if serials:
            selected = (
                self.current_serial if self.current_serial in self.devices else serials[0]
            )
            if selected != self.current_serial:
                self.current_serial = selected
                self._reset_device_state()
            device = self.devices[selected]
            self._update_device_label()
            self.status_var.set("Đã kết nối thiết bị.")
            self._log(f"Thiết bị: {device.display_name} ({selected})")
            self._load_packages()
        else:
            # Không xảy ra khi kiểm tra kết nối đã báo sẵn sàng.
            self.current_serial = None
            self._reset_device_state()
            self._update_device_label()
            self.status_var.set("Không có thiết bị sẵn sàng.")
            self._log("Không có thiết bị sẵn sàng.")

    def _next_device(self) -> None:
        if self.device_loading or self.uninstalling or len(self.devices) < 2:
            return
        serials = list(self.devices)
        index = serials.index(self.current_serial) if self.current_serial in serials else -1
        self.current_serial = serials[(index + 1) % len(serials)]
        self._reset_device_state()
        self._update_device_label()
        device = self.devices[self.current_serial]
        self._log(f"Đổi sang thiết bị: {device.display_name} ({self.current_serial})")
        self._load_packages()

    def _on_filter_selected(self, _event: tk.Event) -> None:
        self._load_packages()

    def _render_apps(self, *_args: object, reset_scroll: bool = True) -> None:
        visible_apps = [
            self.apps[package]
            for package in self.packages
            if package in self.apps
        ]
        if self.search_var.get().strip():
            self.app_list.empty_text = "Không tìm thấy ứng dụng phù hợp."
        else:
            self.app_list.empty_text = "Không có ứng dụng nào để hiển thị."
        self.app_list.set_apps(
            visible_apps,
            self.search_var.get(),
            reset_scroll=reset_scroll,
        )
        self.app_list.set_selected_packages(self.selected_packages)
        self.count_var.set(f"{len(self.app_list.apps)} ứng dụng")
        self._update_selection_controls()

    def _on_visible_apps_changed(self, packages: list[str]) -> None:
        if self.visible_job:
            self.root.after_cancel(self.visible_job)
        self.visible_job = self.root.after(
            180, lambda: self._queue_visible_metadata(packages)
        )

    def _queue_visible_metadata(self, packages: list[str]) -> None:
        if not self.current_serial or self.uninstalling:
            return
        self._enqueue_metadata(packages, self.PRIORITY_VISIBLE)

    # -------------------------------------------------------------- search
    def _on_search_changed(self, *_args: object) -> None:
        self._render_apps()
        if self.search_job:
            self.root.after_cancel(self.search_job)
        self.search_job = self.root.after(
            SEARCH_DEBOUNCE_MS, self._start_search_metadata
        )

    def _start_search_metadata(self) -> None:
        """Chỉ đọc tên nền khi đang tìm kiếm, và chỉ cho ứng dụng thật sự cần.

        - Hủy việc nền của từ khóa trước.
        - Bỏ qua ứng dụng có package đã khớp từ khóa (đã được liệt kê rồi).
        - Bỏ qua ứng dụng đã có tên hoặc không có biểu tượng mở được.
        """
        if self.search_job:
            self.root.after_cancel(self.search_job)
            self.search_job = 0
        self.bg_token += 1
        self.metadata_queued_bg.clear()
        self.search_needed = set()

        serial = self.current_serial
        tokens = fold_text(self.search_var.get()).split()
        if not tokens or not serial or self.uninstalling or not self.metadata_available:
            self._update_metadata_status()
            return

        needed = [
            package
            for package in self.packages
            if self._wants_metadata(package)
            and (serial, package) not in self.metadata_attempted
            and not all(token in fold_text(package) for token in tokens)
        ]
        self.search_needed = {(serial, package) for package in needed}
        queued = self._enqueue_metadata(needed, self.PRIORITY_BACKGROUND)
        if queued:
            self._log(f"Đọc tên {queued} ứng dụng để phục vụ tìm kiếm.")
        self._update_metadata_status()

    # ----------------------------------------------------------- selection
    def _toggle_visible_selection(self) -> None:
        visible = self.app_list.get_listed_packages()
        if self.select_visible_var.get():
            self.selected_packages.update(visible)
        else:
            self.selected_packages.difference_update(visible)
        self.app_list.set_selected_packages(self.selected_packages)
        self._update_controls()

    def _clear_selection(self) -> None:
        self.selected_packages.clear()
        self.app_list.set_selected_packages(set())
        self._update_controls()

    def _on_selection_changed(self) -> None:
        self.selected_packages = self.app_list.get_selected_packages()
        self._update_controls()

    def _update_selection_controls(self) -> None:
        visible = self.app_list.get_listed_packages()
        self.select_visible_var.set(
            bool(visible) and visible.issubset(self.selected_packages)
        )
        self.selected_count_var.set(f"Đã chọn {len(self.selected_packages)}")

    def _update_metadata_status(self) -> None:
        serial = self.current_serial
        if self.uninstalling or not serial or not self.packages:
            return
        if not self.metadata_available:
            self.status_var.set(
                "Chưa có aapt nên chưa đọc được tên ứng dụng; vẫn tìm được theo package."
            )
            return
        if self.search_needed:
            done = sum(1 for key in self.search_needed if key in self.metadata_attempted)
            if done < len(self.search_needed):
                self.status_var.set(
                    f"Đang đọc tên ứng dụng để tìm kiếm {done}/{len(self.search_needed)}..."
                )
                return
        self.status_var.set(f"Đã tải {len(self.packages)} ứng dụng.")

    # ----------------------------------------------------------- uninstall
    def uninstall_selected(self) -> None:
        packages = sorted(self.selected_packages)
        serial = self.current_serial
        if not packages or not serial or self.uninstalling:
            return

        device_name = self.devices[serial].display_name if serial in self.devices else serial
        preview = "\n".join(packages[:8])
        if len(packages) > 8:
            preview += f"\n... và {len(packages) - 8} ứng dụng khác"
        confirmed = messagebox.askyesno(
            "Xác nhận gỡ nhiều ứng dụng",
            f"Gỡ {len(packages)} ứng dụng khỏi {device_name}?\n\n"
            f"{preview}\n\nThao tác này có thể làm mất dữ liệu ứng dụng. "
            "Ứng dụng hệ thống sẽ được gỡ cho người dùng hiện tại.",
            icon="warning",
            parent=self.root,
        )
        if not confirmed:
            return

        self.uninstalling = True
        self.status_var.set(f"Đang chuẩn bị gỡ {len(packages)} ứng dụng...")
        self._log(f"Bắt đầu gỡ {len(packages)} ứng dụng trên {device_name}.")
        self.progress.configure(maximum=max(len(packages), 1), value=0)
        self.progress.grid()
        self._update_controls()
        threading.Thread(
            target=self._run_operation,
            args=(lambda: self._uninstall_batch(serial, packages), serial),
            daemon=True,
        ).start()

    def _uninstall_batch(
        self, serial: str, packages: list[str]
    ) -> BatchUninstallResult:
        """Gỡ, kiểm tra lại trên máy, và gỡ lại những ứng dụng còn sót."""
        max_attempts = self.UNINSTALL_ATTEMPTS
        pending = list(packages)
        errors: dict[str, str] = {}
        attempts_used = 0

        for attempt in range(1, max_attempts + 1):
            attempts_used = attempt
            total = len(pending)
            for index, package in enumerate(pending, start=1):
                self.events.put(
                    BatchUninstallProgress(
                        index, total, package, attempt, max_attempts
                    )
                )
                try:
                    self.adb.uninstall(serial, package)
                    errors.pop(package, None)
                except AdbError as exc:
                    errors[package] = str(exc)

            self.events.put(
                BatchUninstallProgress(
                    total, total, "", attempt, max_attempts, verifying=True
                )
            )
            time.sleep(self.VERIFY_DELAY)
            try:
                installed = self.adb.installed_packages(serial)
            except AdbError:
                # Không kiểm tra được: dựa vào kết quả của lệnh gỡ.
                pending = [package for package in pending if package in errors]
                break
            pending = [package for package in pending if package in installed]
            if not pending:
                break

        failed: list[tuple[str, str]] = []
        for package in pending:
            if package in errors:
                message = (
                    f"Vẫn còn trên thiết bị sau {attempts_used} lần gỡ. "
                    f"{errors[package]}"
                )
            else:
                message = (
                    f"Vẫn còn trên thiết bị sau {attempts_used} lần gỡ "
                    "(lệnh gỡ báo thành công nhưng ứng dụng chưa biến mất)."
                )
            failed.append((package, message))

        still_installed = {package for package, _ in failed}
        successful = tuple(p for p in packages if p not in still_installed)
        return BatchUninstallResult(
            serial, successful, tuple(failed), attempts_used
        )

    @staticmethod
    def _batch_summary(result: BatchUninstallResult) -> str:
        ok = len(result.successful)
        fail = len(result.failed)
        if not fail:
            text = f"Đã gỡ và xác nhận {ok} ứng dụng không còn trên thiết bị."
            if result.attempts > 1:
                text += f" (cần {result.attempts} lần thử)"
            return text
        return f"Hoàn tất: {ok} đã gỡ, {fail} vẫn còn trên thiết bị."

    def _show_batch_result(self, result: BatchUninstallResult) -> None:
        message = self._batch_summary(result)
        if result.failed:
            errors = "\n".join(
                f"{package}: {error}" for package, error in result.failed[:8]
            )
            if len(result.failed) > 8:
                errors += f"\n... và {len(result.failed) - 8} lỗi khác"
            message += (
                f"\n\nKhông gỡ được {len(result.failed)} ứng dụng:\n{errors}\n\n"
                "Các ứng dụng này vẫn được chọn để bạn thử lại."
            )
            messagebox.showwarning("Kết quả gỡ ứng dụng", message, parent=self.root)
        else:
            messagebox.showinfo("Kết quả gỡ ứng dụng", message, parent=self.root)

    def _update_controls(self) -> None:
        busy = self.device_loading or self.uninstalling
        self.refresh_button.configure(state="disabled" if busy else "normal")
        self.switch_button.configure(state="disabled" if busy else "normal")
        self.filter_combo.configure(
            state="readonly" if self.current_serial and not busy else "disabled"
        )
        self.search_entry.configure(state="disabled" if busy else "normal")
        self.select_visible.configure(state="disabled" if busy else "normal")
        self.clear_selection_button.configure(
            state="normal" if self.selected_packages and not busy else "disabled"
        )
        count = len(self.selected_packages)
        self.uninstall_button.configure(
            text=f"Gỡ đã chọn ({count})" if count else "Gỡ đã chọn",
            state="normal"
            if self.selected_packages and self.current_serial and not busy
            else "disabled",
        )
        self._update_selection_controls()

    def _close(self) -> None:
        self._cancel_gate_poll()
        while True:
            try:
                self.metadata_queue.get_nowait()
            except queue.Empty:
                break
        self.metadata_queue.put((-1, 0, None, 0))
        self._close_log()
        self.root.destroy()
