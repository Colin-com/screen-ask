"""验收大图查看器：fit / 放大 / 拖动 三态各存一张。"""
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import screen_ask as sa                      # noqa: E402
from PIL import ImageGrab                    # noqa: E402

OUT = Path(tempfile.gettempdir()) / "screen-ask-test"
OUT.mkdir(exist_ok=True)

sa.setup_dpi()
root = tk.Tk()
root.withdraw()
sa.init_fonts(root)
app = sa.App(root)
root.update()
app.capture_and_show()
root.update()
time.sleep(0.3)

app._open_viewer()
root.update()
time.sleep(1.0)
top = app.viewer
cv = None
for child in top.winfo_children():
    for c in child.winfo_children():
        if isinstance(c, tk.Canvas):
            cv = c


def shot(name):
    root.update()
    time.sleep(0.35)
    x, y = top.winfo_rootx(), top.winfo_rooty()
    w, h = top.winfo_width(), top.winfo_height()
    p = OUT / (name + ".png")
    ImageGrab.grab(all_screens=True).crop((x, y, x + w, y + h)).save(p)
    print(name, "->", p)


shot("viewer_fit")

w, h = cv.winfo_width(), cv.winfo_height()
for _ in range(6):
    cv.event_generate("<MouseWheel>", delta=120, x=w // 2, y=h // 2)
    root.update()
time.sleep(0.4)
shot("viewer_zoom")

cv.event_generate("<Button-1>", x=w // 2, y=h // 2)
for i in range(12):
    cv.event_generate("<B1-Motion>", x=w // 2 - i * 9, y=h // 2 - i * 5)
    root.update()
cv.event_generate("<ButtonRelease-1>", x=w // 2 - 108, y=h // 2 - 60)
root.update()
time.sleep(0.4)
shot("viewer_pan")
root.destroy()
print("done")
