# -*- coding: utf-8 -*-
"""
Screen Ask — 按一次热键，截屏，问视觉模型。

界面：工业仪器风（直角 / 1px 细线 / 近黑 + 单一信号红）
依赖：tkinter（标准库）+ Pillow。零第三方运行时依赖。

跨平台说明见 README.md（Windows 全功能；macOS / Linux 部分功能降级）。
"""
from __future__ import annotations

import base64
import io
import json
import os
import queue
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageDraw, ImageGrab, ImageOps, ImageTk

# ---------------------------------------------------------------- 平台

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# ---------------------------------------------------------------- 路径
# 代码目录（只读：模板、README）
APP_DIR = Path(__file__).resolve().parent
# 用户目录（可写：.env、日志）。可用 SCREEN_ASK_HOME 覆盖。
USER_DIR = Path(os.environ.get("SCREEN_ASK_HOME") or (Path.home() / ".screen-ask"))
LOG_PATH = USER_DIR / "run.log"

# ---------------------------------------------------------------- 主题

BG = "#0b0b0d"
PANEL = "#101013"
SUNK = "#08080a"
LINE = "#232329"
LINE2 = "#3a3a42"
INK = "#e6e6e8"
DIM = "#6b6b74"
MUTED = "#8a8a93"
ACCENT = "#ff4a1c"

PH = "问这个画面里的任何东西…"

# 基准尺寸（96 DPI 下的逻辑像素），实际窗口按屏幕与 DPI 自适应
BASE_W, BASE_H = 760, 840
MIN_W, MIN_H = 520, 560

FONT_STACK_SANS = (
    "Microsoft YaHei UI", "微软雅黑", "PingFang SC", "Hiragino Sans GB",
    "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei",
    "Segoe UI", "Helvetica Neue", "Arial",
)
FONT_STACK_MONO = (
    "Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono",
    "Liberation Mono", "Courier New", "Courier",
)

SANS = "TkDefaultFont"
MONO = "TkFixedFont"
PIX = ""          # 点阵字体，仅用于纯 ASCII 标签
PIX_SZ = -20      # 像素单位（负数），不随 DPI 缩放，保住点阵


def init_fonts(root: tk.Misc) -> None:
    """按平台挑可用字体，避免硬编码 Consolas / 雅黑在别的系统上变方块。"""
    global SANS, MONO, PIX, PIX_SZ
    try:
        fams = {f.lower(): f for f in tkfont.families(root)}
    except Exception:
        fams = {}

    def pick(stack, fallback):
        for name in stack:
            if name.lower() in fams:
                return fams[name.lower()]
        return fallback

    SANS = pick(FONT_STACK_SANS, "TkDefaultFont")
    MONO = pick(FONT_STACK_MONO, "TkFixedFont")
    # 点阵字体：Windows 的 Terminal 原生 8x12，只有 8pt / -12px 才不被抗锯齿
    pix = pick(("Terminal", "Fixedsys"), "")
    if pix:
        PIX, PIX_SZ = pix, -20
    else:
        PIX, PIX_SZ = MONO, 7


def setup_dpi() -> None:
    """Windows 上声明 DPI 感知，否则高分屏会被系统拉伸成模糊。"""
    if not IS_WIN:
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def ui_scale(root: tk.Misc) -> float:
    """相对 96 DPI 的缩放系数（1.0 = 100%）。"""
    try:
        return max(0.75, min(3.0, root.winfo_fpixels("1i") / 96.0))
    except Exception:
        return 1.0


# ---------------------------------------------------------------- 日志

def log(msg) -> None:
    try:
        USER_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 配置

def env_candidates() -> list[Path]:
    return [
        USER_DIR / ".env",
        APP_DIR / ".env",
        Path.cwd() / ".env",
    ]


def read_env() -> dict:
    d = {}
    for p in env_candidates():
        try:
            if not p.is_file():
                continue
            for raw in p.read_text(encoding="utf-8").splitlines():
                s = raw.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                d.setdefault(k.strip(), v.strip().strip("\"'"))
        except Exception:
            pass
    # 环境变量优先级最高
    for k, v in os.environ.items():
        if k.startswith(("DEEPSEEK_", "SA_", "OLLAMA_")):
            d[k] = v
    return d


def env_write_path() -> Path:
    """写到已存在的那个 .env；都不存在就新建用户级配置，别往仓库里写。"""
    for p in (USER_DIR / ".env", APP_DIR / ".env"):
        if p.is_file():
            return p
    USER_DIR.mkdir(parents=True, exist_ok=True)
    return USER_DIR / ".env"


def write_env(updates: dict) -> Path:
    path = env_write_path()
    lines, seen = [], set()
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            s = raw.rstrip()
            stripped = s.strip()
            if stripped.startswith("#") and "=" in stripped:
                key = stripped.lstrip("#").split("=", 1)[0].strip()
                if key in updates:
                    lines.append("%s=%s" % (key, updates[key]))
                    seen.add(key)
                    continue
            key = s.split("=", 1)[0].strip() if ("=" in s and not stripped.startswith("#")) else None
            if key in updates:
                lines.append("%s=%s" % (key, updates[key]))
                seen.add(key)
            else:
                lines.append(s)
    for k, v in updates.items():
        if k not in seen:
            lines.append("%s=%s" % (k, v))
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


ENV = read_env()

PROVIDER = (ENV.get("SA_PROVIDER") or "deepseek").lower()
OLLAMA_BASE = (ENV.get("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = ENV.get("SA_OLLAMA_MODEL") or "qwen2.5vl:7b"
DS_BASE = (ENV.get("SA_DEEPSEEK_BASE") or "https://api.deepseek.com").rstrip("/")
DS_MODEL = ENV.get("SA_DEEPSEEK_MODEL") or "deepseek-v4-flash-vision-exp"
DS_KEY = ENV.get("DEEPSEEK_API_KEY") or ""
MODEL_LABEL = OLLAMA_MODEL if PROVIDER == "ollama" else DS_MODEL

HOTKEY_SPEC = ENV.get("SA_HOTKEY") or "ctrl+alt+q"


def deepseek_url() -> str:
    return DS_BASE + "/chat/completions"


# ---------------------------------------------------------------- 热键

def _hotkey_parts(spec: str):
    parts = [p.strip().lower() for p in spec.replace("+", " ").split() if p.strip()]
    mods = 0
    vk = None
    table = {
        "ctrl": 0x2, "control": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8, "cmd": 0x8,
    }
    for p in parts:
        if p in table:
            mods |= table[p]
        elif len(p) == 1:
            vk = ord(p.upper())
        elif p.startswith("f") and p[1:].isdigit():
            vk = 0x70 + int(p[1:]) - 1
    return mods, (vk or 0x51)


def start_hotkey_win(hk_queue: queue.Queue) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    MOD_NOREPEAT = 0x4000
    WM_HOTKEY = 0x0312
    HOTKEY_ID = 0xA11
    mods, vk = _hotkey_parts(HOTKEY_SPEC)

    def run():
        ok = user32.RegisterHotKey(None, HOTKEY_ID, mods | MOD_NOREPEAT, vk)
        log("RegisterHotKey(%s) -> %s" % (HOTKEY_SPEC, ok))
        if not ok:
            hk_queue.put(("hotkey_fail", None))
            return
        hk_queue.put(("hotkey_ok", None))
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                hk_queue.put(("hotkey", None))

    threading.Thread(target=run, daemon=True).start()


def start_hotkey_portable(hk_queue: queue.Queue) -> None:
    """非 Windows：用可选的 keyboard 库。没装就降级为「只能点界面按钮」。"""
    try:
        import keyboard  # type: ignore
    except Exception:
        log("keyboard 未安装，全局热键不可用（可用 SA_HOTKEY 或安装 keyboard）")
        hk_queue.put(("hotkey_fail", None))
        return
    try:
        keyboard.add_hotkey(HOTKEY_SPEC.replace("win", "windows"), lambda: hk_queue.put(("hotkey", None)))
        hk_queue.put(("hotkey_ok", None))
        log("hotkey ready (%s)" % HOTKEY_SPEC)
    except Exception as e:
        log("hotkey fail %s" % e)
        hk_queue.put(("hotkey_fail", None))


def start_hotkey(hk_queue: queue.Queue) -> None:
    if IS_WIN:
        start_hotkey_win(hk_queue)
    else:
        start_hotkey_portable(hk_queue)


# ---------------------------------------------------------------- 截图

def grab_screen() -> Image.Image:
    if IS_WIN:
        try:
            return ImageGrab.grab(all_screens=True)
        except Exception:
            return ImageGrab.grab()
    return ImageGrab.grab()


# ---------------------------------------------------------------- 8bit 渲染
# 一帧的加工链：降采样 → 像素块 → 限色阶 → 扫描线。全部在 PIL 里做，
# 尺寸压在取景器高度（几百像素），所以单帧只要十几毫秒。

def pixelate(im: Image.Image, target_h: int, block: int = 2) -> Image.Image:
    """先缩到 1/block 再放大回来 —— 得到硬边的方块像素，不是模糊插值。"""
    tw = max(8, int(im.width * (target_h / im.height)))
    th = max(8, target_h)
    small = im.resize((max(1, tw // block), max(1, th // block)), Image.BILINEAR)
    return small.resize((tw, th), Image.NEAREST)


def draw_scanlines(im: Image.Image, phase: int = 0, gap: int = 3, alpha: int = 58) -> None:
    d = ImageDraw.Draw(im, "RGBA")
    start = (phase % gap) - gap
    for y in range(start, im.height, gap):
        d.line([(0, y), (im.width, y)], fill=(0, 0, 0, alpha))


def crt_frame(im: Image.Image, target_h: int, block: int = 2,
              reveal: float = 1.0, phase: int = 0, beam: bool = False) -> Image.Image:
    p = pixelate(im, target_h, block)
    p = ImageOps.posterize(p.convert("RGB"), 4)      # 每通道 4bit，8bit 调色板感
    if reveal < 1.0:
        cut = max(1, int(p.height * reveal))
        mask = Image.new("RGB", p.size, (6, 6, 8))
        mask.paste(p.crop((0, 0, p.width, cut)), (0, 0))
        p = mask
        if beam:
            d = ImageDraw.Draw(p)
            d.line([(0, cut - 1), (p.width, cut - 1)], fill=(255, 74, 28))
            if cut > 3:
                d.line([(0, cut - 3), (p.width, cut - 3)], fill=(120, 40, 16))
    draw_scanlines(p, phase)
    return p


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.ui_q: queue.Queue = queue.Queue()
        self.hk_q: queue.Queue = queue.Queue()
        self.messages: list = []
        self.image_b64 = None
        self.full_img: Image.Image | None = None
        self.panel = None
        self.viewer = None
        self.busy = False
        self.photo = None
        self.thumb_img = None
        self._crt_base: Image.Image | None = None
        self._crt_phase = 0
        self._crt_on = True
        self.t0 = 0.0
        self.scale = ui_scale(root)
        self._build()
        start_hotkey(self.hk_q)
        self._warmup()
        root.after(50, self.poll)
        root.after(400, self._crt_tick)

    # ---------------- 启动预热 ----------------
    def _warmup(self):
        if PROVIDER != "ollama":
            return

        def run():
            try:
                req = urllib.request.Request(
                    OLLAMA_BASE + "/api/chat",
                    data=json.dumps({
                        "model": OLLAMA_MODEL,
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": False,
                        "options": {"num_predict": 1},
                    }).encode(),
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=600).read()
                log("warmup ok")
            except Exception as e:
                log("warmup fail %s" % e)

        threading.Thread(target=run, daemon=True).start()

    # ---------------- 界面 ----------------
    def _build(self):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        sc = self.scale

        W = int(max(MIN_W, min(BASE_W * sc, sw * 0.46)))
        H = int(max(MIN_H, min(BASE_H * sc, sh * 0.90)))
        self.W, self.H = W, H
        self.VIEW_H = int(max(180, min(W * 9 / 16, H * 0.52)))

        x = max(16, sw - W - int(48 * sc))
        y = max(24, (sh - H) // 2)

        win = tk.Toplevel(self.root)
        self.win = win
        win.title("Screen Ask")
        if not IS_MAC:                       # macOS 下 overrideredirect 会收不到键盘
            win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.geometry("%dx%d+%d+%d" % (W, H, x, y))
        win.configure(bg=LINE2)              # 1px 外描边
        win.withdraw()

        shell = tk.Frame(win, bg=BG)
        shell.pack(fill="both", expand=True, padx=1, pady=1)

        # --- 顶栏 ---
        bar = tk.Frame(shell, bg=PANEL, height=int(50 * min(sc, 2)))
        bar.pack(fill="x")
        bar.pack_propagate(False)

        tk.Label(bar, text="SCR-01", bg=PANEL, fg=MUTED,
                 font=(PIX, PIX_SZ)).pack(side="left", padx=(14, 0))
        tk.Frame(bar, bg=LINE2, width=1, height=20).pack(side="left", padx=12, pady=15)
        tk.Label(bar, text="屏幕问答", bg=PANEL, fg=INK,
                 font=(SANS, 12, "bold")).pack(side="left")

        close = tk.Label(bar, text="✕", bg=PANEL, fg=DIM, font=(MONO, 13), cursor="hand2")
        close.pack(side="right", padx=(0, 14))
        self.status = tk.Label(bar, text="READY", bg=PANEL, fg=MUTED, font=(PIX, PIX_SZ))
        self.status.pack(side="right", padx=(0, 20))
        self.api_lbl = tk.Label(bar, text="API", bg=PANEL, fg=MUTED, font=(PIX, PIX_SZ), cursor="hand2")
        self.api_lbl.pack(side="right", padx=(0, 20))
        self.model_lbl = tk.Label(bar, text=self._model_text(), bg=PANEL, fg=MUTED, font=(PIX, PIX_SZ))
        self.model_lbl.pack(side="right", padx=(0, 20))

        self.api_lbl.bind("<Button-1>", lambda e: self._open_api_panel())
        self.api_lbl.bind("<Enter>", lambda e: self.api_lbl.configure(fg=ACCENT))
        self.api_lbl.bind("<Leave>", lambda e: self.api_lbl.configure(fg=MUTED))
        close.bind("<Button-1>", lambda e: self.hide())
        close.bind("<Enter>", lambda e: close.configure(fg=ACCENT))
        close.bind("<Leave>", lambda e: close.configure(fg=DIM))

        for wdg in (bar, self.status, self.model_lbl):
            wdg.bind("<Button-1>", self._drag_start)
            wdg.bind("<B1-Motion>", self._drag_move)

        tk.Frame(shell, bg=LINE, height=1).pack(fill="x")

        # --- 取景器 ---
        self.view = tk.Frame(shell, bg=SUNK, height=self.VIEW_H,
                             highlightthickness=1, highlightbackground=LINE2)
        self.view.pack(fill="x")
        self.view.pack_propagate(False)
        self.thumb = tk.Label(self.view, bg=SUNK, bd=0, cursor="hand2")
        self.thumb.place(relx=0.5, rely=0.5, anchor="center")
        self.thumb.bind("<Button-1>", lambda e: self._open_viewer())

        dw = tk.Frame(self.view, bg=LINE2)
        dw.place(x=10, y=10)
        self.dim_tag = tk.Label(dw, text="--", bg=PANEL, fg=MUTED, font=(PIX, PIX_SZ), padx=9, pady=5)
        self.dim_tag.pack(padx=1, pady=1)

        rw = tk.Frame(self.view, bg=LINE2)
        rw.place(relx=1.0, x=-10, y=10, anchor="ne")
        self.recap = tk.Label(rw, text="RECAPTURE", bg=PANEL, fg=INK, font=(PIX, PIX_SZ),
                              padx=9, pady=5, cursor="hand2")
        self.recap.pack(padx=1, pady=1)
        self.recap.bind("<Button-1>", lambda e: self.resnap())
        self.recap.bind("<Enter>", lambda e: self.recap.configure(bg=ACCENT, fg="#0b0b0d"))
        self.recap.bind("<Leave>", lambda e: self.recap.configure(bg=PANEL, fg=INK))

        tk.Frame(shell, bg=LINE, height=1).pack(fill="x")

        # --- 对话流 ---
        self.text = tk.Text(shell, bg=BG, fg=INK, bd=0, highlightthickness=0,
                            wrap="word", height=6, font=(SANS, 12),
                            padx=18, pady=4, spacing1=0, spacing2=2, spacing3=0,
                            insertbackground=ACCENT, cursor="arrow")
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("you", font=(PIX, PIX_SZ), foreground=ACCENT, spacing1=16)
        self.text.tag_configure("ai", font=(PIX, PIX_SZ), foreground=DIM, spacing1=16)
        self.text.tag_configure("body", font=(SANS, 12), foreground=INK,
                                spacing1=4, spacing3=10, lmargin1=0, lmargin2=0)
        self.text.tag_configure("meta", font=(PIX, PIX_SZ), foreground=DIM, spacing3=10)
        self.text.tag_configure("err", font=(MONO, 8), foreground=ACCENT, spacing3=10)
        self.text.configure(state="disabled")

        tk.Frame(shell, bg=LINE, height=1).pack(fill="x")

        # --- 输入区 ---
        foot = tk.Frame(shell, bg=PANEL)
        foot.pack(fill="x")

        self.entry = tk.Text(foot, bg=PANEL, fg=INK, bd=0, highlightthickness=0,
                             height=3, width=1, font=(SANS, 11), padx=18, pady=12,
                             insertbackground=ACCENT, wrap="word",
                             selectbackground=LINE2, selectforeground=INK)
        self.entry.pack(side="left", fill="both", expand=True)
        self.entry.bind("<Return>", self._on_enter)
        self.entry.bind("<Shift-Return>", lambda e: None)
        self.entry.bind("<Tab>", lambda e: "break")
        self.entry.bind("<Key>", self._ph_key, add="+")
        self._placeholder()

        btnwrap = tk.Frame(foot, bg=LINE2)
        btnwrap.pack(side="right", padx=(8, 18), pady=(10, 0), anchor="n")
        self.send_btn = tk.Label(btnwrap, text="SEND", bg=PANEL, fg=ACCENT,
                                 font=(PIX, PIX_SZ), padx=22, pady=12, cursor="hand2")
        self.send_btn.pack(padx=1, pady=1)
        self.send_btn.bind("<Button-1>", lambda e: self._on_send())
        self.send_btn.bind("<Enter>", lambda e: self._btn_hover(True))
        self.send_btn.bind("<Leave>", lambda e: self._btn_hover(False))

        tk.Label(foot, text="ENTER SEND    ESC HIDE    ^R RECAP", bg=PANEL, fg=MUTED,
                 font=(PIX, PIX_SZ)).pack(side="bottom", anchor="w", padx=18, pady=(0, 10))

        win.bind("<Escape>", lambda e: (self._close_panel() if self.panel else self.hide()))
        win.bind("<Control-r>", lambda e: self.resnap())
        win.bind("<Control-q>", lambda e: self.hide())

        if not DS_KEY and PROVIDER != "ollama":
            self.set_status("NO KEY", ACCENT)

    def _model_text(self) -> str:
        """顶栏空间有限时截断模型名，但保留可辨识前缀。"""
        name = MODEL_LABEL
        limit = 32 if self.W >= 640 else 18
        return name if len(name) <= limit else name[: limit - 1] + "…"

    def _btn_hover(self, on):
        if self.busy:
            return
        self.send_btn.configure(bg=ACCENT if on else PANEL, fg="#0b0b0d" if on else ACCENT)

    def _placeholder(self):
        self.entry.insert("1.0", PH)
        self.entry.configure(fg=MUTED)

    def _ph_key(self, e=None):
        if self.entry.get("1.0", "end").strip() == PH:
            self.entry.delete("1.0", "end")
            self.entry.configure(fg=INK)

    def _ph_set(self, e=None):
        if not self.entry.get("1.0", "end").strip():
            self.entry.insert("1.0", PH)
            self.entry.configure(fg=MUTED)

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y()

    def _drag_move(self, e):
        self.win.geometry("+%d+%d" % (e.x_root - self._dx, e.y_root - self._dy))

    # ---------------- 大图查看 ----------------
    def _open_viewer(self):
        if self.full_img is None:
            return
        if self.viewer is not None:
            try:
                self.viewer.lift()
                return
            except Exception:
                self.viewer = None

        im = self.full_img
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        vw = int(max(480, min(sw * 0.86, im.width * 1.05)))
        vh = int(max(360, min(sh * 0.86, im.height * 1.05)))

        top = tk.Toplevel(self.root)
        self.viewer = top
        if not IS_MAC:
            top.overrideredirect(True)
        top.title("Screen Ask · Viewer")
        top.attributes("-topmost", True)
        top.configure(bg=LINE2)
        top.geometry("%dx%d+%d+%d" % (vw, vh, (sw - vw) // 2, (sh - vh) // 2))

        inner = tk.Frame(top, bg=BG)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        head = tk.Frame(inner, bg=PANEL, height=34)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="VIEWER", bg=PANEL, fg=DIM, font=(PIX, PIX_SZ)).pack(side="left", padx=(14, 0))
        tk.Label(head, text="%d x %d" % im.size, bg=PANEL, fg=DIM,
                 font=(PIX, PIX_SZ)).pack(side="left", padx=12)
        zoom_lbl = tk.Label(head, text="FIT", bg=PANEL, fg=DIM, font=(PIX, PIX_SZ))
        zoom_lbl.pack(side="right", padx=(0, 14))
        if vw >= 900:
            tk.Label(head, text="SCROLL ZOOM / DRAG PAN / DBLCLICK FIT", bg=PANEL, fg=LINE2,
                     font=(PIX, PIX_SZ)).pack(side="right", padx=(0, 12))
        cls = tk.Label(head, text="✕", bg=PANEL, fg=DIM, font=(MONO, 10), cursor="hand2")
        cls.pack(side="right", padx=(0, 10))
        cls.bind("<Button-1>", lambda e: top.destroy())
        tk.Frame(inner, bg=LINE, height=1).pack(fill="x")

        cv = tk.Canvas(inner, bg=SUNK, highlightthickness=0, cursor="fleur")
        cv.pack(fill="both", expand=True, padx=8, pady=8)

        st = {"s": 1.0, "fit": 1.0, "dx": 0.0, "dy": 0.0,
              "px": 0, "py": 0, "ph": None, "job": None, "job2": None}

        def render(sharp: bool):
            """只绘制画布可见的那一块——放大到 800% 也不会生成巨大的位图。"""
            s = st["s"]
            cw = cv.winfo_width() or vw
            ch = cv.winfo_height() or vh
            dw, dh = im.width * s, im.height * s
            x0 = cw / 2.0 + st["dx"] - dw / 2.0
            y0 = ch / 2.0 + st["dy"] - dh / 2.0
            vx0, vy0 = max(0.0, x0), max(0.0, y0)
            vx1, vy1 = min(float(cw), x0 + dw), min(float(ch), y0 + dh)
            cv.delete("all")
            if vx1 - vx0 < 1 or vy1 - vy0 < 1:
                return
            bx0 = max(0, int((vx0 - x0) / s))
            by0 = max(0, int((vy0 - y0) / s))
            bx1 = min(im.width, int((vx1 - x0) / s) + 2)
            by1 = min(im.height, int((vy1 - y0) / s) + 2)
            if bx1 <= bx0 or by1 <= by0:
                return
            crop = im.crop((bx0, by0, bx1, by1))
            tw, th = max(1, int(vx1 - vx0)), max(1, int(vy1 - vy0))
            if sharp:
                # 精修：缩小用 LANCZOS 保细节，放大用 BILINEAR 够快
                filt = Image.LANCZOS if s < 0.75 else Image.BILINEAR
            else:
                # 即时反馈：放大用 NEAREST（本来就要补精修），缩小用 BILINEAR 防锯齿
                filt = Image.NEAREST if s >= 1.0 else Image.BILINEAR
            st["ph"] = ImageTk.PhotoImage(crop.resize((tw, th), filt))
            cv.create_image(int(vx0), int(vy0), image=st["ph"], anchor="nw")

        def render_soft():
            # 快速档：先在 24ms 后给一次「跟手」的画面
            st["job"] = None
            render(False)
            if st["job2"]:
                try:
                    top.after_cancel(st["job2"])
                except Exception:
                    pass
            st["job2"] = top.after(180, render_sharp_now)

        def render_sharp_now():
            st["job2"] = None
            render(True)

        def schedule_soft():
            """滚轮连发时合并渲染——10 格滚轮只重绘最后那一帧。"""
            if st["job"]:
                try:
                    top.after_cancel(st["job"])
                except Exception:
                    pass
            st["job"] = top.after(24, render_soft)

        def do_fit(*_):
            cw, ch = cv.winfo_width(), cv.winfo_height()
            if cw < 50 or ch < 50:
                cw, ch = vw - 2, vh - 36
            st["fit"] = min(cw / im.width, ch / im.height)
            st["s"] = st["fit"]
            st["dx"] = st["dy"] = 0.0
            zoom_lbl.configure(text="FIT")
            render(True)

        def zoom_at(px, py, delta):
            f = 1.12 if delta > 0 else 1 / 1.12
            new_s = max(0.05, min(10.0, st["s"] * f))
            f = new_s / st["s"]
            cw, ch = cv.winfo_width(), cv.winfo_height()
            mx, my = px - cw / 2.0, py - ch / 2.0
            st["dx"] = mx - (mx - st["dx"]) * f
            st["dy"] = my - (my - st["dy"]) * f
            st["s"] = new_s
            zoom_lbl.configure(text="%d%%" % round(new_s * 100))
            schedule_soft()

        def on_wheel(e):
            zoom_at(e.x, e.y, 120 if e.delta > 0 else -120)

        def on_mouse_down(e):
            st["px"], st["py"] = e.x, e.y

        def on_drag(e):
            dx, dy = e.x - st["px"], e.y - st["py"]
            st["dx"] += dx
            st["dy"] += dy
            st["px"], st["py"] = e.x, e.y
            cv.move("all", dx, dy)   # 拖动期间只是平移画布，不重采样

        def on_release(_e):
            render(True)

        cv.bind("<MouseWheel>", on_wheel)                                   # Windows / macOS
        cv.bind("<Button-4>", lambda e: zoom_at(e.x, e.y, 120))             # X11 滚轮上
        cv.bind("<Button-5>", lambda e: zoom_at(e.x, e.y, -120))            # X11 滚轮下
        cv.bind("<Button-1>", on_mouse_down)
        cv.bind("<B1-Motion>", on_drag)
        cv.bind("<ButtonRelease-1>", on_release)
        cv.bind("<Double-Button-1>", do_fit)

        top.bind("<Escape>", lambda e: top.destroy())
        top.bind("<Control-w>", lambda e: top.destroy())
        head.bind("<Button-1>", lambda e: (st.__setitem__("wx", e.x_root - top.winfo_x()),
                                           st.__setitem__("wy", e.y_root - top.winfo_y())))
        head.bind("<B1-Motion>", lambda e: top.geometry(
            "+%d+%d" % (e.x_root - st.get("wx", 0), e.y_root - st.get("wy", 0))))

        rjob = {"id": None}

        def on_resize(_e):
            if rjob["id"]:
                try:
                    top.after_cancel(rjob["id"])
                except Exception:
                    pass
            rjob["id"] = top.after(140, do_fit)

        cv.bind("<Configure>", on_resize)
        top.after(60, do_fit)
        top.focus_force()

    # ---------------- 接口设置 ----------------
    def _open_api_panel(self):
        if self.panel is not None:
            return
        p = tk.Frame(self.win, bg=BG)
        p.place(x=1, y=1, relwidth=1.0, relheight=1.0)
        self.panel = p

        head = tk.Frame(p, bg=PANEL, height=38)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="CONFIG", bg=PANEL, fg=DIM, font=(PIX, PIX_SZ)).pack(side="left", padx=(14, 0))
        tk.Frame(head, bg=LINE2, width=1, height=14).pack(side="left", padx=10, pady=12)
        tk.Label(head, text="接口设置", bg=PANEL, fg=INK,
                 font=(SANS, 10, "bold")).pack(side="left")
        xb = tk.Label(head, text="✕", bg=PANEL, fg=DIM, font=(MONO, 10), cursor="hand2")
        xb.pack(side="right", padx=(0, 14))
        xb.bind("<Button-1>", lambda e: self._close_panel())
        tk.Frame(p, bg=LINE, height=1).pack(fill="x")

        foot = tk.Frame(p, bg=PANEL)
        foot.pack(side="bottom", fill="x")
        tk.Frame(foot, bg=LINE, height=1).pack(fill="x")
        row = tk.Frame(foot, bg=PANEL)
        row.pack(fill="x", padx=18, pady=12)
        self.pmsg = tk.Label(row, text="保存写入 %s" % env_write_path().name,
                             bg=PANEL, fg=DIM, font=(MONO, 7))
        self.pmsg.pack(side="left")
        applyb = tk.Label(row, text="APPLY", bg=ACCENT, fg="#0b0b0d",
                          font=(PIX, PIX_SZ), padx=22, pady=9, cursor="hand2")
        applyb.pack(side="right")
        applyb.bind("<Button-1>", lambda e: self._apply_config())

        body = tk.Frame(p, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=16)

        tk.Label(body, text="PROVIDER", bg=BG, fg=DIM, font=(PIX, PIX_SZ)).pack(anchor="w")
        tabs = tk.Frame(body, bg=BG)
        tabs.pack(anchor="w", pady=(6, 18))
        self.pv = tk.StringVar(value=PROVIDER)
        self.tab_map = {}
        for key, txt in (("ollama", "OLLAMA  本地"), ("deepseek", "DEEPSEEK  云端")):
            holder = tk.Frame(tabs, bg=LINE2)
            holder.pack(side="left", padx=(0, 8))
            lb = tk.Label(holder, text=txt, bg=PANEL, fg=DIM, font=(PIX, PIX_SZ),
                          padx=12, pady=6, cursor="hand2")
            lb.pack(padx=1, pady=1)
            lb.bind("<Button-1>", lambda e, k=key: self._pick_provider(k))
            self.tab_map[key] = lb

        def field(label, default, show=None):
            tk.Label(body, text=label, bg=BG, fg=DIM, font=(PIX, PIX_SZ)).pack(anchor="w", pady=(0, 4))
            wrap = tk.Frame(body, bg=LINE)
            wrap.pack(fill="x", pady=(0, 14))
            e = tk.Entry(wrap, bg=PANEL, fg=INK, bd=0, highlightthickness=0,
                         font=(SANS, 12), insertbackground=ACCENT, show=show,
                         disabledbackground=SUNK, disabledforeground=DIM,
                         readonlybackground=SUNK, selectbackground=LINE2)
            e.pack(fill="x", padx=1, pady=1, ipady=6)
            e.insert(0, default or "")
            return e

        self.f_model = field("MODEL", OLLAMA_MODEL if PROVIDER == "ollama" else DS_MODEL)
        self.f_url = field("BASE URL", OLLAMA_BASE if PROVIDER == "ollama" else DS_BASE)
        self.f_key = field("API KEY", DS_KEY, show="•")
        self._pick_provider(PROVIDER)

    def _pick_provider(self, k):
        self.pv.set(k)
        for key, lb in self.tab_map.items():
            lb.configure(bg=ACCENT if key == k else PANEL, fg="#0b0b0d" if key == k else DIM)
        if k == "ollama":
            self.f_model.configure(state="normal")
            self.f_model.delete(0, "end")
            self.f_model.insert(0, OLLAMA_MODEL)
            self.f_url.configure(state="normal")
            self.f_url.delete(0, "end")
            self.f_url.insert(0, OLLAMA_BASE)
            self.f_key.configure(state="normal")
            self.f_key.delete(0, "end")
            self.f_key.insert(0, "本地模型不需要 key")
            self.f_key.configure(state="disabled")
        else:
            self.f_model.configure(state="normal")
            self.f_model.delete(0, "end")
            self.f_model.insert(0, DS_MODEL)
            self.f_url.configure(state="normal")
            self.f_url.delete(0, "end")
            self.f_url.insert(0, DS_BASE)
            self.f_key.configure(state="normal")
            self.f_key.delete(0, "end")
            self.f_key.insert(0, DS_KEY)

    def _close_panel(self):
        if self.panel is not None:
            try:
                self.panel.destroy()
            except Exception:
                pass
        self.panel = None

    def _apply_config(self):
        global PROVIDER, OLLAMA_MODEL, OLLAMA_BASE, DS_MODEL, DS_BASE, DS_KEY, MODEL_LABEL
        k = self.pv.get()
        model = self.f_model.get().strip()
        url = self.f_url.get().strip().rstrip("/")
        key = self.f_key.get().strip()
        try:
            if k == "ollama":
                OLLAMA_MODEL = model or OLLAMA_MODEL
                OLLAMA_BASE = url or OLLAMA_BASE
                upd = {"SA_PROVIDER": "ollama", "SA_OLLAMA_MODEL": OLLAMA_MODEL,
                       "OLLAMA_URL": OLLAMA_BASE}
            else:
                DS_MODEL = model or DS_MODEL
                DS_BASE = url or DS_BASE
                if key and key != "本地模型不需要 key":
                    DS_KEY = key
                upd = {"SA_PROVIDER": "deepseek", "SA_DEEPSEEK_MODEL": DS_MODEL,
                       "SA_DEEPSEEK_BASE": DS_BASE, "DEEPSEEK_API_KEY": DS_KEY}
            PROVIDER = k
            MODEL_LABEL = OLLAMA_MODEL if k == "ollama" else DS_MODEL
            write_env(upd)
        except Exception as e:
            self.pmsg.configure(text="保存失败：%s" % e, fg=ACCENT)
            return

        self.model_lbl.configure(text=self._model_text())
        self.messages = []
        self._close_panel()
        self.clear_chat()
        self._insert("CONFIG APPLIED\n", "meta")
        self._insert("%s · %s\n" % (PROVIDER, MODEL_LABEL), "meta")
        self.set_status("APPLIED", ACCENT)
        log("config applied provider=%s model=%s" % (PROVIDER, MODEL_LABEL))
        if PROVIDER == "ollama":
            self._warmup()

    # ---------------- 截图 ----------------
    def _grab(self):
        img = grab_screen()
        raw = img.size
        img = img.convert("RGB")
        small = img.copy()
        small.thumbnail((1600, 1600), Image.LANCZOS)
        buf = io.BytesIO()
        small.save(buf, "JPEG", quality=78)
        self.image_b64 = base64.b64encode(buf.getvalue()).decode()
        return img, raw

    def resnap(self, *_):
        self.win.withdraw()
        self.root.update()
        time.sleep(0.18)
        self.capture_and_show()

    def capture_and_show(self):
        self.set_status("CAPTURING", ACCENT)
        try:
            img, size = self._grab()
        except Exception as e:
            log("grab fail %s" % e)
            self.set_status("CAPTURE FAILED", ACCENT)
            self.win.deiconify()
            self._insert("截图失败：%s\n（macOS 需在 系统设置 → 隐私 → 屏幕录制 里授权）\n" % e, "err")
            return
        self.full_img = img
        self.messages = []
        self.dim_tag.configure(text="%d x %d" % size)

        self.clear_chat()
        self.set_status("READY", DIM)
        self._ph_set()
        self.entry.focus_set()
        self.win.deiconify()
        self.win.lift()
        self._scan_in(img)

    # ---------------- 8bit 渲染管道 ----------------
    def _paint(self, pim):
        """把一张已经加工好的 PIL 图贴进取景器（唯一碰 ImageTk 的地方）。"""
        ph = ImageTk.PhotoImage(pim)
        self.thumb.configure(image=ph)
        self.thumb_img = ph

    def _scan_in(self, img, steps=9, interval=40):
        """CRT 接入：画面按行从上往下扫出来，扫到底再叠扫描线。"""
        base = crt_frame(img, self.VIEW_H, block=2)
        self._crt_base = base
        self._crt_phase = 0
        w, h = base.size

        def step(i):
            if i > steps:
                self._paint(base)
                return
            cut = max(2, int(h * i / steps))
            frame = Image.new("RGB", base.size, (6, 6, 8))
            frame.paste(base.crop((0, 0, w, cut)), (0, 0))
            d = ImageDraw.Draw(frame)
            d.line([(0, cut - 1), (w, cut - 1)], fill=(255, 74, 28))
            if cut > 4:
                d.line([(0, cut - 4), (w, cut - 4)], fill=(110, 36, 14))
            self._paint(frame)
            self.root.after(interval, lambda: step(i + 1))

        step(1)

    def _crt_tick(self):
        """静态时扫描线缓慢下移——画面是「活的」，但不重新解码整张图。"""
        if (self._crt_on and self._crt_base is not None
                and self.win.winfo_viewable() and not self.busy):
            self._crt_phase = (self._crt_phase + 1) % 3
            frame = self._crt_base.copy()
            draw_scanlines(frame, self._crt_phase)
            self._paint(frame)
        self.root.after(260, self._crt_tick)

    # ---------------- 开机动画 ----------------
    def boot_then_capture(self):
        self.win.deiconify()
        self.win.lift()
        self.boot_anim(lambda: self.capture_and_show())

    def boot_anim(self, done):
        cover = tk.Frame(self.win, bg=BG)
        cover.place(x=0, y=0, relwidth=1, relheight=1)
        t = tk.Text(cover, bg=BG, fg=MUTED, bd=0, highlightthickness=0,
                    font=(PIX, PIX_SZ), padx=26, pady=22, spacing1=3, spacing3=3,
                    cursor="arrow")
        t.pack(fill="both", expand=True)
        t.configure(state="disabled")
        t.tag_configure("hdr", foreground=ACCENT, font=(PIX, PIX_SZ))
        t.tag_configure("dim", foreground=LINE2, font=(PIX, PIX_SZ))
        t.tag_configure("ok", foreground=DIM, font=(PIX, PIX_SZ))
        t.tag_configure("bar", foreground=ACCENT)

        rows = [
            ("SCR-01 // SCREEN ASK", "hdr"),
            ("8-bit capture console  v0.3", "dim"),
            ("", None),
            ("> display ....... %d x %d @ %d%%" % (
                self.root.winfo_screenwidth(), self.root.winfo_screenheight(),
                round(self.scale * 100)), "ok"),
            ("> fonts ......... %s / %s" % (SANS, MONO), "ok"),
            ("> provider ...... %s" % PROVIDER, "ok"),
            ("> model ......... %s" % MODEL_LABEL, "ok"),
            ("> hotkey ........ %s" % HOTKEY_SPEC, "ok"),
            ("> pipeline ...... 8BIT / CRT / SCAN", "ok"),
            ("", None),
        ]

        state = {"i": 0}

        def tick():
            i = state["i"]
            if i < len(rows):
                txt, tag = rows[i]
                t.configure(state="normal")
                if tag:
                    t.insert("end", txt + "\n", tag)
                else:
                    t.insert("end", "\n")
                if i >= 3:
                    t.insert("end", "", "bar")
                t.see("end")
                t.configure(state="disabled")
                state["i"] = i + 1
                self.root.after(58 if txt else 26, tick)
                return
            # 进度条 + READY 闪烁
            t.configure(state="normal")
            for k in range(18):
                t.insert("end", "█", "bar")
            t.insert("end", "\n\n", "bar")
            t.insert("end", "[ FRAMEBUFFER OK ]  READY", "hdr")
            t.configure(state="disabled")
            self.root.after(420, finish)

        def finish():
            try:
                cover.destroy()
            except Exception:
                pass
            done()

        self.root.after(90, tick)

    def clear_chat(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", "CAPTURED\n", "meta")
        self.text.insert("end", "画面已截，直接问。追问不用重新截。\n", "meta")
        self.text.configure(state="disabled")

    def set_status(self, txt, color):
        self.status.configure(text=txt, fg=color)

    # ---------------- 对话 ----------------
    def _on_enter(self, e):
        if e.state & 0x1:
            return None
        self._on_send()
        return "break"

    def _on_send(self):
        if self.busy:
            return
        q = self.entry.get("1.0", "end").strip()
        if not q or q == PH:
            return
        if self.image_b64 is None:
            self.capture_and_show()
        self.entry.delete("1.0", "end")
        self._ph_set()
        self.busy = True
        self.send_btn.configure(bg=PANEL, fg=DIM, text="WAIT")
        self.set_status("STREAMING", ACCENT)

        self.text.configure(state="normal")
        self.text.insert("end", "YOU\n", "you")
        self.text.insert("end", q + "\n", "body")
        self.text.insert("end", (MODEL_LABEL.split(":")[0].upper() if PROVIDER == "ollama" else "MODEL") + "\n",
                         "ai")
        self.text.configure(state="disabled")
        self.text.see("end")

        if not self.messages:
            user_msg = {"role": "user", "content": q, "images": [self.image_b64]}
        else:
            user_msg = {"role": "user", "content": q}
        self.messages.append(user_msg)
        self.t0 = time.time()
        threading.Thread(target=self._worker, args=(list(self.messages),), daemon=True).start()

    @staticmethod
    def _to_openai(msgs):
        out = []
        for m in msgs:
            imgs = m.get("images") or []
            if imgs:
                content = [{"type": "text", "text": m.get("content", "")}]
                content += [{"type": "image_url",
                             "image_url": {"url": "data:image/jpeg;base64," + b}} for b in imgs]
                out.append({"role": m["role"], "content": content})
            else:
                out.append({"role": m["role"], "content": m.get("content", "")})
        return out

    def _worker(self, msgs):
        acc = ""
        try:
            if PROVIDER == "ollama":
                payload = {
                    "model": OLLAMA_MODEL,
                    "messages": msgs,
                    "stream": True,
                    "options": {"temperature": 0.4, "num_ctx": 4096, "num_predict": 1200},
                }
                req = urllib.request.Request(
                    OLLAMA_BASE + "/api/chat",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=600) as r:
                    for raw in r:
                        line = raw.decode("utf-8", "ignore").strip()
                        if not line:
                            continue
                        try:
                            j = json.loads(line)
                        except Exception:
                            continue
                        d = (j.get("message") or {}).get("content") or ""
                        if d:
                            acc += d
                            self.ui_q.put(("delta", d))
                        if j.get("done"):
                            break
            else:
                if not DS_KEY:
                    raise RuntimeError("缺少 API KEY —— 点顶栏 API 填写")
                payload = {
                    "model": DS_MODEL,
                    "messages": self._to_openai(msgs),
                    "stream": True,
                    "max_tokens": 3000,
                    "thinking": {"type": "disabled"},
                }
                req = urllib.request.Request(
                    deepseek_url(),
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + DS_KEY},
                )
                with urllib.request.urlopen(req, timeout=180) as r:
                    for raw in r:
                        line = raw.decode("utf-8", "ignore").strip()
                        if not line.startswith("data:"):
                            continue
                        d = line[5:].strip()
                        if not d or d == "[DONE]":
                            continue
                        try:
                            j = json.loads(d)
                        except Exception:
                            continue
                        ch = j.get("choices") or [{}]
                        delta = (ch[0].get("delta") or {}).get("content")
                        if delta:
                            acc += delta
                            self.ui_q.put(("delta", delta))
            self.ui_q.put(("done", acc))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            self.ui_q.put(("err", "HTTP %s %s" % (e.code, body)))
        except Exception as e:
            self.ui_q.put(("err", "%s: %s" % (type(e).__name__, e)))

    def _insert(self, s, tag):
        self.text.configure(state="normal")
        self.text.insert("end", s, tag)
        self.text.configure(state="disabled")
        self.text.see("end")

    def poll(self):
        try:
            while True:
                kind, payload = self.ui_q.get_nowait()
                if kind == "delta":
                    self._insert(payload, "body")
                elif kind == "done":
                    self.messages.append({"role": "assistant", "content": payload})
                    self.busy = False
                    self.send_btn.configure(bg=PANEL, fg=ACCENT, text="SEND")
                    el = time.time() - self.t0
                    if not payload:
                        self._insert("(空回复)\n", "err")
                    else:
                        self._insert("\n", "body")
                    self.set_status("READY · %.1fs" % el, DIM)
                elif kind == "err":
                    self.busy = False
                    self.send_btn.configure(bg=PANEL, fg=ACCENT, text="SEND")
                    self._insert("\n" + payload + "\n", "err")
                    self.set_status("ERROR", ACCENT)
        except queue.Empty:
            pass

        try:
            while True:
                kind, _ = self.hk_q.get_nowait()
                if kind == "hotkey":
                    if self.win.winfo_viewable():
                        self.hide()
                    else:
                        self.resnap()
                elif kind == "hotkey_fail":
                    self.set_status("NO HOTKEY", ACCENT)
                elif kind == "hotkey_ok":
                    log("hotkey ready")
        except queue.Empty:
            pass
        self.root.after(60, self.poll)

    def hide(self, *_):
        self.win.withdraw()


def main():
    setup_dpi()
    root = tk.Tk()
    root.withdraw()
    root.title("Screen Ask")
    init_fonts(root)
    log("--- start %s | provider=%s model=%s ---" % (sys.platform, PROVIDER, MODEL_LABEL))
    app = App(root)
    root.after(120, app.boot_then_capture)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log(traceback.format_exc())
        raise
