"""验收 token 标签：流式中只报输出、结束后补 API usage、多轮累计。"""
import sys
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import screen_ask as sa

sa.setup_dpi()
root = tk.Tk()
root.withdraw()
app = sa.App(root)


def pump(sec):
    t = time.time()
    while time.time() - t < sec:
        root.update()
        time.sleep(0.01)


pump(0.3)
app.capture_and_show()
pump(1.2)

print("idle:", repr(app.tok_lbl.cget("text")), app.tok_lbl.cget("fg"))

app.entry.delete("1.0", "end")
app.entry.insert("1.0", "用一句话说这张图里是什么")
app._on_send()

# 流式进行中
live_txt, live_fg, live_chars = "", "", 0
for _ in range(60):
    pump(0.05)
    if app._tok_live:
        live_txt = app.tok_lbl.cget("text")
        live_fg = app.tok_lbl.cget("fg")
        live_chars = app._tok_shown
    elif live_chars:
        break
print("live:", repr(live_txt), live_fg, "chars shown:", live_chars)

# 等完成
for _ in range(120):
    pump(0.05)
    if not app.busy:
        break
pump(0.2)
print("after:", repr(app.tok_lbl.cget("text")), app.tok_lbl.cget("fg"))
print("cumulative:", app.tok_in, app.tok_out)

# 第二轮：累计应叠加
app.entry.delete("1.0", "end")
app.entry.insert("1.0", "再说一句")
app._on_send()
for _ in range(120):
    pump(0.05)
    if not app.busy:
        break
pump(0.2)
print("round2:", repr(app.tok_lbl.cget("text")))
print("cumulative:", app.tok_in, app.tok_out)

app.win.destroy()
root.destroy()
