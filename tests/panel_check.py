"""截一张 API 面板，验收 6 个 provider 标签与字段。"""
import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import screen_ask as sa

OUT = Path.home() / "AppData/Local/Temp/screen-ask-test"
OUT.mkdir(parents=True, exist_ok=True)

sa.setup_dpi()
root = tk.Tk()
app = sa.App(root)


def shot():
    try:
        img = sa.grab_screen()
        img.save(str(OUT / "panel_full.png"))
        print("shot", img.size)
    finally:
        root.quit()


root.after(100, app.boot_then_capture)
root.after(2200, app._open_api_panel)
root.after(3000, shot)
root.mainloop()
