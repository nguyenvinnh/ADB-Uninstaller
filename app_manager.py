from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

from adb_app_manager.adb import AdbClient, find_adb
from adb_app_manager.ui import AppManager


def main() -> None:
    root = tk.Tk()
    try:
        adb = AdbClient(find_adb())
    except FileNotFoundError as exc:
        messagebox.showerror("Không tìm thấy ADB", str(exc), parent=root)
        root.destroy()
        return
    AppManager(root, adb)
    root.mainloop()


if __name__ == "__main__":
    main()
