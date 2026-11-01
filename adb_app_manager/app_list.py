from __future__ import annotations

import tkinter as tk
import unicodedata
import zlib
from tkinter import ttk
from typing import Callable

from .models import AppInfo

ACCENT = "#2563eb"
ROW_BG = "#ffffff"
ROW_HOVER_BG = "#f6f8fb"
ROW_SELECTED_BG = "#eef4ff"
SEPARATOR = "#eef1f5"
TITLE_COLOR = "#1f2937"
DETAIL_COLOR = "#7b8494"

# (nền avatar, màu chữ)
AVATAR_COLORS = [
    ("#e0ecff", "#1d4ed8"),
    ("#e3f6ec", "#15803d"),
    ("#fdecd8", "#b45309"),
    ("#f3e8ff", "#7e22ce"),
    ("#fde4e8", "#be123c"),
    ("#e0f4f7", "#0e7490"),
]


def fold_text(text: str) -> str:
    """Chuẩn hóa để tìm kiếm: bỏ dấu tiếng Việt, không phân biệt hoa thường."""
    text = text.replace("đ", "d").replace("Đ", "D")
    normalized = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in normalized if not unicodedata.combining(c))
    return stripped.casefold()


class AppList(ttk.Frame):
    ROW_HEIGHT = 56
    CHECK_SIZE = 16
    AVATAR_SIZE = 34

    def __init__(
        self,
        master: tk.Misc,
        on_selection_changed: Callable[[], None],
        on_visible_changed: Callable[[list[str]], None],
    ) -> None:
        super().__init__(master)
        self.on_selection_changed = on_selection_changed
        self.on_visible_changed = on_visible_changed
        self.apps: list[AppInfo] = []
        self.selected_packages: set[str] = set()
        self.hover_index: int | None = None
        self.anchor_index: int | None = None
        self.empty_text = "Không có ứng dụng nào để hiển thị."
        self._last_visible: list[str] | None = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            self,
            highlightthickness=1,
            highlightbackground="#d9dee7",
            highlightcolor="#d9dee7",
            borderwidth=0,
            background=ROW_BG,
            yscrollincrement=self.ROW_HEIGHT // 2,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(
            self, orient=tk.VERTICAL, command=self.yview
        )
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.bind("<Configure>", self._render)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)

    # ------------------------------------------------------------------ API
    def set_apps(
        self, apps: list[AppInfo], query: str = "", reset_scroll: bool = True
    ) -> None:
        tokens = fold_text(query).split()
        if tokens:
            self.apps = [
                app
                for app in apps
                if all(
                    token in haystack
                    for haystack in (fold_text(f"{app.label} {app.package}"),)
                    for token in tokens
                )
            ]
        else:
            self.apps = list(apps)
        self.anchor_index = None
        self.hover_index = None
        self._last_visible = None
        if reset_scroll:
            self.canvas.yview_moveto(0)
        self._render()

    def set_selected_packages(self, packages: set[str]) -> None:
        self.selected_packages = set(packages)
        self._render()

    def set_empty_text(self, text: str) -> None:
        self.empty_text = text
        self._render()

    def get_selected_packages(self) -> set[str]:
        return set(self.selected_packages)

    def get_listed_packages(self) -> set[str]:
        return {app.package for app in self.apps}

    def yview(self, *args: str) -> None:
        self.canvas.yview(*args)
        self._render()

    # --------------------------------------------------------------- events
    def _index_at(self, y: int) -> int | None:
        index = int(self.canvas.canvasy(y) // self.ROW_HEIGHT)
        if 0 <= index < len(self.apps):
            return index
        return None

    def _on_mousewheel(self, event: tk.Event) -> str:
        step = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(step * 3, "units")
        self.hover_index = None
        self._render()
        return "break"

    def _on_motion(self, event: tk.Event) -> None:
        index = self._index_at(event.y)
        self.canvas.configure(cursor="hand2" if index is not None else "")
        if index != self.hover_index:
            self.hover_index = index
            self._render()

    def _on_leave(self, _event: tk.Event) -> None:
        if self.hover_index is not None:
            self.hover_index = None
            self._render()

    def _on_click(self, event: tk.Event) -> None:
        index = self._index_at(event.y)
        if index is None:
            return
        shift = bool(event.state & 0x0001)
        if shift and self.anchor_index is not None and self.anchor_index < len(self.apps):
            start, end = sorted((self.anchor_index, index))
            self.selected_packages.update(
                app.package for app in self.apps[start : end + 1]
            )
        else:
            package = self.apps[index].package
            if package in self.selected_packages:
                self.selected_packages.remove(package)
            else:
                self.selected_packages.add(package)
            self.anchor_index = index
        self._render()
        self.on_selection_changed()

    # ------------------------------------------------------------ rendering
    @staticmethod
    def _truncate(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return text[: max(max_chars - 1, 1)] + "…"

    @staticmethod
    def _avatar_colors(package: str) -> tuple[str, str]:
        return AVATAR_COLORS[zlib.crc32(package.encode("utf-8")) % len(AVATAR_COLORS)]

    @staticmethod
    def _avatar_letter(title: str) -> str:
        for char in title:
            if char.isalnum():
                return char.upper()
        return "?"

    def _render(self, _event: tk.Event | None = None) -> None:
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        row_h = self.ROW_HEIGHT
        self.canvas.configure(
            scrollregion=(0, 0, width, max(len(self.apps) * row_h, height))
        )
        self.canvas.delete("all")

        if not self.apps:
            self.canvas.create_text(
                width / 2,
                height / 2,
                text=self.empty_text,
                fill=DETAIL_COLOR,
                font=("Segoe UI", 10),
            )
            self._notify_visible([])
            return

        top = max(int(self.canvas.canvasy(0) // row_h) - 1, 0)
        bottom = min(int(self.canvas.canvasy(height) // row_h) + 2, len(self.apps))
        max_chars = max((width - 110) // 7, 10)
        check = self.CHECK_SIZE
        avatar = self.AVATAR_SIZE

        for index in range(top, bottom):
            app = self.apps[index]
            y = index * row_h
            mid = y + row_h / 2
            checked = app.package in self.selected_packages
            if checked:
                background = ROW_SELECTED_BG
            elif index == self.hover_index:
                background = ROW_HOVER_BG
            else:
                background = ROW_BG

            self.canvas.create_rectangle(
                0, y, width, y + row_h, fill=background, outline=""
            )
            self.canvas.create_line(
                16, y + row_h - 1, width, y + row_h - 1, fill=SEPARATOR
            )
            if checked:
                self.canvas.create_rectangle(
                    0, y + 6, 3, y + row_h - 6, fill=ACCENT, outline=""
                )

            # Ô tích nhỏ
            cx = 18
            cy = mid - check / 2
            self.canvas.create_rectangle(
                cx,
                cy,
                cx + check,
                cy + check,
                fill=ACCENT if checked else "#ffffff",
                outline=ACCENT if checked else "#a8b0bd",
                width=1,
            )
            if checked:
                self.canvas.create_line(
                    cx + 3.5,
                    cy + 8.5,
                    cx + 6.5,
                    cy + 11.5,
                    cx + 12.5,
                    cy + 4.5,
                    fill="#ffffff",
                    width=2,
                    capstyle=tk.ROUND,
                    joinstyle=tk.ROUND,
                )

            # Avatar chữ cái đầu
            has_label = app.label != app.package
            title = app.label if has_label else app.package
            avatar_bg, avatar_fg = self._avatar_colors(app.package)
            ax = 46
            self.canvas.create_oval(
                ax,
                mid - avatar / 2,
                ax + avatar,
                mid + avatar / 2,
                fill=avatar_bg,
                outline="",
            )
            self.canvas.create_text(
                ax + avatar / 2,
                mid,
                text=self._avatar_letter(title),
                fill=avatar_fg,
                font=("Segoe UI", 11, "bold"),
            )

            text_x = ax + avatar + 12
            detail = app.package if has_label else "Chưa có tên hiển thị"
            self.canvas.create_text(
                text_x,
                mid - 10,
                text=self._truncate(title, max_chars),
                anchor="w",
                fill=TITLE_COLOR,
                font=("Segoe UI", 10, "bold"),
            )
            self.canvas.create_text(
                text_x,
                mid + 11,
                text=self._truncate(detail, max_chars + 6),
                anchor="w",
                fill=DETAIL_COLOR,
                font=("Segoe UI", 9),
            )

        self._notify_visible([app.package for app in self.apps[top:bottom]])

    def _notify_visible(self, packages: list[str]) -> None:
        if packages != self._last_visible:
            self._last_visible = packages
            self.on_visible_changed(packages)
