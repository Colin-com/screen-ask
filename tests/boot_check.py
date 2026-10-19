"""验收开机动画与 8bit 渲染：boot 三段 + 扫描入场 + 静态帧。"""
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import screen_ask as sa
from PIL import ImageGrab

OUT = Path(tempfile.gettempdir()) / "screen-ask-test"
OUT.mkdir(parents=True, exist_ok=True)

sa.setup_dpi()
root = tk.Tk()
root.withdraw()
sa.init_fonts(root)
app = sa.App(root)


def pump(sec):
    end = time.time() + sec
    while time.time() < end:
        root.update()
        time.sleep(0.008)


def shot(name):
    root.update()
    x, y = app.win.winfo_rootx(), app.win.winfo_rooty()
    w, h = app.win.winfo_width(), app.win.winfo_height()
    p = OUT / (name + ".png")
    ImageGrab.grab(all_screens=True).crop((x, y, x + w, y + h)).save(p)
    print("%-12s %dx%d -> %s" % (name, w, h, p))


app.boot_then_capture()
pump(0.30)
shot("boot_1")
pump(0.45)
shot("boot_2")
pump(0.35)
shot("scan")
pump(0.80)
shot("final")
print("base frame:", app._crt_base.size if app._crt_base else None)
root.destroy()
