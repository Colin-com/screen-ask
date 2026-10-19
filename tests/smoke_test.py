"""冒烟测试：截图 → 问答 → 查看器缩放性能 → 取证截图。

用法（在项目根目录）：
    python tests/smoke_test.py
产物写到系统临时目录。
"""
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
print("capture ok | full=%s | scale=%.2f | model=%s"
      % (app.full_img.size, app.scale, sa.MODEL_LABEL))

app.entry.delete("1.0", "end")
app.entry.insert("1.0", "这一屏主要是什么内容？一句话")
app._on_send()
t0 = time.time()
while app.busy and time.time() - t0 < 60:
    root.update()
    time.sleep(0.02)
print("ask done in %.1fs" % (time.time() - t0))

app._open_viewer()
root.update()
time.sleep(0.6)
cv = None
for child in app.viewer.winfo_children():
    for c in child.winfo_children():
        if isinstance(c, tk.Canvas):
            cv = c

if cv:
    w, h = cv.winfo_width(), cv.winfo_height()
    print("viewer canvas %dx%d" % (w, h))
    t0 = time.time()
    for _ in range(30):
        cv.event_generate("<MouseWheel>", delta=120, x=w // 2, y=h // 2)
        root.update()
    time.sleep(0.35)          # 等合并后的精修渲染落定
    root.update()
    t_in = time.time() - t0
    t0 = time.time()
    for _ in range(30):
        cv.event_generate("<MouseWheel>", delta=-120, x=w // 2, y=h // 2)
        root.update()
    time.sleep(0.35)
    root.update()
    t_out = time.time() - t0
    print("zoom in : %.0f ms per 30-step burst" % (t_in * 1000))
    print("zoom out: %.0f ms per 30-step burst" % (t_out * 1000))
    t0 = time.time()
    for i in range(30):
        cv.event_generate("<B1-Motion>", x=w // 2 + i, y=h // 2 + i)
        root.update()
    print("pan     : %.1f ms/step" % ((time.time() - t0) / 30 * 1000))
else:
    print("canvas not found")

if app.viewer is not None:
    app.viewer.destroy()
    app.viewer = None
root.update()
time.sleep(0.4)

x, y = app.win.winfo_rootx(), app.win.winfo_rooty()
w, h = app.win.winfo_width(), app.win.winfo_height()
shot = OUT / "window.png"
ImageGrab.grab(all_screens=True).crop((x, y, x + w, y + h)).save(shot)
print("shot ->", shot)
root.destroy()
