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

# DeepSeek 官方定价（USD / 1M tokens）
# 来源 api-docs.deepseek.com/quick_start/pricing，2026-10 核对
# 高峰 = UTC 01:00-04:00 与 06:00-10:00 的工作日（= 北京 09-12 / 14-18）
# 中国法定节假日未排除，节假日会被按高峰计价，略高估
PRICE = {
    "peak":    (0.006, 0.30, 1.2),   # (cache hit, cache miss, output)
    "offpeak": (0.003, 0.15, 0.6),
}
USD_CNY = 7.15


def is_peak(now=None):
    import datetime as _dt
    t = now or _dt.datetime.now(_dt.timezone.utc)
    if t.weekday() >= 5:
        return False
    h = t.hour + t.minute / 60.0
    return (1.0 <= h < 4.0) or (6.0 <= h < 10.0)


def resolve_price(model=None):
    """返回 (输入, 输出, 缓存命中) USD/1M；未知模型返回 None，宁可显示 ¥— 也不瞎猜。"""
    env_in = ENV.get("SA_PRICE_IN")
    env_out = ENV.get("SA_PRICE_OUT")
    if env_in and env_out:
        try:
            return (float(env_in), float(env_out), float(ENV.get("SA_PRICE_CACHED") or env_in))
        except ValueError:
            pass
    table = prov().get("pricing") or {}
    name = (model or DS_MODEL or "").lower()
    hit = None
    for k in table:
        if k.lower() in name and (hit is None or len(k) > len(hit)):
            hit = k
    if hit is None:
        return None
    return table[hit]


def token_cost(prompt, completion, cache_hit=0, model=None):
    """按 provider 价格表计价，返回美元；未知模型返回 None。"""
    price = resolve_price(model)
    if price is None:
        return None
    p_in, p_out, p_hit = price
    mult = 1.0
    if prov().get("peak_mult") and is_peak():
        mult = float(prov()["peak_mult"])
    prompt = int(prompt or 0)
    completion = int(completion or 0)
    hit = max(0, min(int(cache_hit or 0), prompt))
    miss = prompt - hit
    return (hit * p_hit + miss * p_in + completion * p_out) * mult / 1e6

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
        if k.startswith(("DEEPSEEK_", "SA_", "OLLAMA_", "OPENAI_", "ANTHROPIC_")):
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

# 各家预设：api 决定协议（openai / anthropic / ollama）
# pricing = {模型名包含的关键字: (输入 USD/1M, 输出 USD/1M, 缓存命中 USD/1M)}
# 查不到模型就返回 None，界面显示 ¥—，不瞎猜价格
PROVIDERS = {
    "deepseek": {
        "label": "DEEPSEEK", "api": "openai",
        "base": "https://api.deepseek.com", "model": "deepseek-flash",
        "peak_mult": 2.0,       # 官方：off-peak 是 peak 的一半
        "pricing": {
            "deepseek-flash": (0.15, 0.6, 0.003),
            "deepseek-v4-flash": (0.15, 0.6, 0.003),
            "deepseek-v4-pro": (0.66, 1.98, 0.022),
        },
    },
    "openai": {
        "label": "OPENAI", "api": "openai",
        "base": "https://api.openai.com/v1", "model": "gpt-6.1-sol",
        "pricing": {
            "gpt-6-astra": (10.0, 50.0, 1.0),
            "gpt-6.1-sol": (2.0, 10.0, 0.1),
            "gpt-6-luna": (0.1, 0.5, 0.01),
            "gpt-5.6-sol": (4.0, 20.0, 0.4),
        },
    },
    "anthropic": {
        "label": "CLAUDE", "api": "anthropic",
        "base": "https://api.anthropic.com", "model": "claude-opus-5-5",
        "pricing": {
            "claude-opus-5-5": (4.0, 20.0, 0.4),
            "claude-sonnet-5-5": (2.0, 10.0, 0.2),
            "claude-haiku-5-5": (0.1, 0.5, 0.01),
            "claude-fable-5-1": (10.0, 50.0, 1.0),
        },
    },
    "openrouter": {
        "label": "OPENROUTER", "api": "openai",
        "base": "https://openrouter.ai/api/v1", "model": "anthropic/claude-opus-5.5",
        "pricing": {},
    },
    "ollama": {
        "label": "OLLAMA", "api": "ollama",
        "base": "http://127.0.0.1:11434", "model": "qwen2.5vl:7b",
        "pricing": {},
    },
    "custom": {
        "label": "CUSTOM", "api": "openai",
        "base": "", "model": "", "pricing": {},
    },
}

PROVIDER = (ENV.get("SA_PROVIDER") or "deepseek").lower()
if PROVIDER not in PROVIDERS:
    PROVIDER = "deepseek"


_KEY_ENV = {"deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY"}


def key_for(k):
    return ENV.get(_KEY_ENV.get(k, "")) or ""


def prov(k=None) -> dict:
    return PROVIDERS.get(k or PROVIDER) or PROVIDERS["deepseek"]


# 统一键优先，旧键兜底（老用户的 .env 照样能用）
OLLAMA_BASE = (ENV.get("OLLAMA_URL") or PROVIDERS["ollama"]["base"]).rstrip("/")
OLLAMA_MODEL = ENV.get("SA_OLLAMA_MODEL") or PROVIDERS["ollama"]["model"]
_legacy_key = {"deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY",
               "anthropic": "ANTHROPIC_API_KEY"}.get(PROVIDER, "DEEPSEEK_API_KEY")
DS_BASE = (ENV.get("SA_BASE")
           or (ENV.get("SA_DEEPSEEK_BASE") if PROVIDER == "deepseek" else None)
           or (OLLAMA_BASE if PROVIDER == "ollama" else None)
           or prov()["base"]).rstrip("/")
DS_MODEL = (ENV.get("SA_MODEL")
            or (ENV.get("SA_OLLAMA_MODEL") if PROVIDER == "ollama" else None)
            or (ENV.get("SA_DEEPSEEK_MODEL") if PROVIDER == "deepseek" else None)
            or prov()["model"])
DS_KEY = ENV.get("SA_KEY") or ENV.get(_legacy_key) or ""
MODEL_LABEL = DS_MODEL

HOTKEY_SPEC = ENV.get("SA_HOTKEY") or "ctrl+alt+q"


def endpoint(path="/chat/completions", base=None) -> str:
    """拼端点。用户把完整路径也填进 BASE URL 时不会拼出双段。"""
    b = (base or DS_BASE or "").rstrip("/")
    for suf in ("/chat/completions", "/messages"):
        if b.endswith(suf):
            b = b[: -len(suf)]
    return b + path


def deepseek_url() -> str:
    return endpoint()


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


# ---------------------------------------------------------------- LaTeX 公式渲染
# 极简 TeX 子集 -> Pillow 图像（零 TeX 依赖），供聊天框把公式渲染成图片。
# 覆盖：上下标 / frac / sqrt / 希腊 / 运算符 / sum·int 上下限 / left-right /
#       矩阵·cases / text·mathrm / hat·vec·overline。失败一律回退原文。

import re
from PIL import Image as _PILImage, ImageDraw as _PILDraw, ImageFont

SS = 3  # 超采样倍数

def _resolve(cands):
    for path, idx in cands:
        try:
            if Path(path).exists():
                return (path, idx)
        except Exception:
            pass
    return None


_FONT_CANDS = {
    "math": [
        ("C:/Windows/Fonts/cambria.ttc", 1),
        ("/System/Library/Fonts/Supplemental/STIXTwoMath.otf", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
    ],
    "serif": [
        ("C:/Windows/Fonts/cambria.ttc", 0),
        ("C:/Windows/Fonts/times.ttf", 0),
        ("/System/Library/Fonts/Times.ttc", 0),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", 0),
    ],
    "cjk": [
        ("C:/Windows/Fonts/msyh.ttc", 0),
        ("C:/Windows/Fonts/simhei.ttf", 0),
        ("/System/Library/Fonts/PingFang.ttc", 0),
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
        ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
    ],
}

F_MATH = _resolve(_FONT_CANDS["math"])
F_SERIF = _resolve(_FONT_CANDS["serif"]) or F_MATH
F_CJK = _resolve(_FONT_CANDS["cjk"]) or F_SERIF
F_SYM = F_MATH

_fcache: dict = {}


def _font(kind, size):
    size = max(4, int(round(size)))
    key = (kind, size)
    f = _fcache.get(key)
    if f is None:
        path, idx = kind
        try:
            f = ImageFont.truetype(path, size, index=idx)
        except Exception:
            f = ImageFont.truetype(path, size)
        _fcache[key] = f
    return f


def _mk(w, h):
    return _PILImage.new("L", (max(1, int(round(w))), max(1, int(round(h)))), 0)


# --------------------------------------------------------------------------
# 原子 Box
# --------------------------------------------------------------------------
def textbox(s, size, kind=F_MATH, dy=0.0):
    """基线左对齐渲染一段文本 -> Box。dy 为基础位移（正=上移）。"""
    if not s:
        return (_mk(1, 1), 0.0, 1.0, 0.0)
    f = _font(kind, size)
    try:
        x0, y0, x1, y1 = f.getbbox(s, anchor="ls")
    except Exception:
        x0, y0, x1, y1 = f.getbbox(s)
    w = max(1.0, x1 - x0)
    h = max(1.0, y1 - y0)
    img = _mk(w, h)
    _PILDraw.Draw(img).text((-x0, -y0), s, font=f, fill=255, anchor="ls")
    return (img, w, h, -y0 + dy)


def hcat(boxes):
    """水平拼接，基线对齐。"""
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return (_mk(1, 1), 0.0, 1.0, 0.0)
    asc = max(b[3] for b in boxes)
    desc = max(b[2] - b[3] for b in boxes)
    W = sum(b[1] for b in boxes)
    H = asc + desc
    img = _mk(W, H)
    x = 0.0
    for m, w, h, a in boxes:
        img.paste(m, (int(round(x)), int(round(asc - a))), m)
        x += w
    return (img, W, H, asc)


def vcat(boxes, gap=0.0, align="center"):
    """垂直堆叠（align: center/left/right）。"""
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return (_mk(1, 1), 0.0, 1.0, 0.0)
    W = max(b[1] for b in boxes)
    H = sum(b[2] for b in boxes) + gap * (len(boxes) - 1)
    img = _mk(W, H)
    y = 0.0
    for m, w, h, a in boxes:
        dx = 0.0 if align == "left" else (W - w if align == "right" else (W - w) / 2.0)
        img.paste(m, (int(round(dx)), int(round(y))), m)
        y += h + gap
    return (img, W, H, boxes[0][3])


def shift(box, dy):
    """正 dy = 基线上移；只改 asc，画布不动，hcat 会正确对齐。"""
    img, w, h, asc = box
    return (img, w, h, asc + dy)


def pad(box, l=0.0, r=0.0, t=0.0, b=0.0):
    img, w, h, asc = box
    out = _mk(w + l + r, h + t + b)
    out.paste(img, (int(round(l)), int(round(t))), img)
    return (out, w + l + r, h + t + b, asc + t)


def scale_box(box, k):
    img, w, h, asc = box
    if k == 1.0:
        return box
    nw, nh = max(1, int(round(w * k))), max(1, int(round(h * k)))
    return (img.resize((nw, nh), _PILImage.LANCZOS), float(nw), float(nh), asc * k)


# --------------------------------------------------------------------------
# 符号表
# --------------------------------------------------------------------------
GREEK = {
    "alpha": "\u03b1", "beta": "\u03b2", "gamma": "\u03b3", "delta": "\u03b4",
    "epsilon": "\u03f5", "varepsilon": "\u03b5", "zeta": "\u03b6", "eta": "\u03b7",
    "theta": "\u03b8", "vartheta": "\u03d1", "iota": "\u03b9", "kappa": "\u03ba",
    "lambda": "\u03bb", "mu": "\u03bc", "nu": "\u03bd", "xi": "\u03be",
    "pi": "\u03c0", "varpi": "\u03d6", "rho": "\u03c1", "varrho": "\u03f1",
    "sigma": "\u03c3", "varsigma": "\u03c2", "tau": "\u03c4", "upsilon": "\u03c5",
    "phi": "\u03d5", "varphi": "\u03c6", "chi": "\u03c7", "psi": "\u03c8",
    "omega": "\u03c9",
    "Gamma": "\u0393", "Delta": "\u0394", "Theta": "\u0398", "Lambda": "\u039b",
    "Xi": "\u039e", "Pi": "\u03a0", "Sigma": "\u03a3", "Upsilon": "\u03a5",
    "Phi": "\u03a6", "Psi": "\u03a8", "Omega": "\u03a9",
}

SYM = {
    "cdot": "\u00b7", "times": "\u00d7", "div": "\u00f7", "pm": "\u00b1",
    "mp": "\u2213", "le": "\u2264", "leq": "\u2264", "ge": "\u2265", "geq": "\u2265",
    "ne": "\u2260", "neq": "\u2260", "approx": "\u2248", "equiv": "\u2261",
    "sim": "\u223c", "simeq": "\u2243", "propto": "\u221d", "infty": "\u221e",
    "partial": "\u2202", "nabla": "\u2207", "angle": "\u2220", "perp": "\u22a5",
    "parallel": "\u2225", "circ": "\u2218", "bullet": "\u2219", "ast": "\u2217",
    "to": "\u2192", "rightarrow": "\u2192", "leftarrow": "\u2190",
    "Rightarrow": "\u21d2", "Leftarrow": "\u21d0", "leftrightarrow": "\u2194",
    "Leftrightarrow": "\u21d4", "mapsto": "\u21a6", "uparrow": "\u2191",
    "downarrow": "\u2193", "in": "\u2208", "notin": "\u2209", "subset": "\u2282",
    "subseteq": "\u2286", "supset": "\u2283", "cup": "\u222a", "cap": "\u2229",
    "emptyset": "\u2205", "forall": "\u2200", "exists": "\u2203", "neg": "\u00ac",
    "land": "\u2227", "lor": "\u2228", "wedge": "\u2227", "vee": "\u2228",
    "oplus": "\u2295", "otimes": "\u2297", "sum": "\u2211", "prod": "\u220f",
    "int": "\u222b", "iint": "\u222c", "iiint": "\u222d", "oint": "\u222e",
    "sqrt": "\u221a", "ldots": "\u2026", "dots": "\u2026", "cdots": "\u22ef",
    "vdots": "\u22ee", "ddots": "\u22f1", "prime": "\u2032", "degree": "\u00b0",
    "hbar": "\u210f", "ell": "\u2113", "aleph": "\u2135", "Re": "\u211c", "Im": "\u2111",
    "langle": "\u27e8", "rangle": "\u27e9", "mid": "\u2223", "vert": "|",
    "Vert": "\u2016", "triangle": "\u25b3", "square": "\u25a1",
    "because": "\u2235", "therefore": "\u2234", "dagger": "\u2020",
    "pmb": "\u00b1", "star": "\u22c6", "setminus": "\u2216",
}

SPACES = {",": 0.17, ":": 0.22, ";": 0.28, "!": -0.17, " ": 0.25,
          "quad": 0.9, "qquad": 1.8, "enspace": 0.5, "thinspace": 0.18}

FUNCS = {"sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan",
         "sinh", "cosh", "tanh", "log", "ln", "lg", "exp", "lim", "max", "min",
         "sup", "inf", "det", "dim", "ker", "gcd", "deg", "arg", "Pr", "mod"}

BIGOPS = {"sum", "prod", "int", "iint", "iiint", "oint", "lim", "bigcup", "bigcap"}
INTEGRALS = {"int", "iint", "iiint", "oint"}

DELIMS = {"(": "(", ")": ")", "[": "[", "]": "]", "\\{": "{", "\\}": "}",
          "|": "|", "\\|": "\u2016", ".": None, "\\langle": "\u27e8",
          "\\rangle": "\u27e9", "\\lvert": "|", "\\rvert": "|"}

MATRIX_ENVS = {"matrix", "pmatrix", "bmatrix", "vmatrix", "Bmatrix", "cases",
               "aligned", "array"}


# --------------------------------------------------------------------------
# 词法
# --------------------------------------------------------------------------
TOK_RE = re.compile(r"\\[a-zA-Z]+\*?|\\\\|\\[^a-zA-Z]|\\.|\d+(?:\.\d+)?|\s+|.", re.S)


def tokenize(s):
    return TOK_RE.findall(s)


# --------------------------------------------------------------------------
# 语法 -> 节点树
# --------------------------------------------------------------------------
class P:
    def __init__(self, toks):
        self.t = toks
        self.i = 0

    def peek(self, k=0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else None

    def next(self):
        v = self.peek()
        self.i += 1
        return v

    def parse(self, stop=()):
        """解析到 } / \right / \end / $ 之类停止符。"""
        out = []
        while True:
            tk = self.peek()
            if tk is None:
                break
            if tk in stop:
                break
            if tk in ("}", "\\right", "\\end", "$", "$$"):
                break
            node = self.parse_one()
            if node is not None:
                out.append(node)
        return out

    # 读一个参数：{...} 或多个 token
    def arg(self):
        tk = self.peek()
        if tk is None:
            return []
        if tk == "{":
            self.next()
            inner = self.parse(stop=("}",))
            if self.peek() == "}":
                self.next()
            return inner
        if tk == "\\":  # 防呆
            self.next()
            return []
        node = self.parse_one()
        return [node] if node else []

    def parse_one(self):
        tk = self.next()
        if tk is None:
            return None
        if tk == "{":
            inner = self.parse(stop=("}",))
            if self.peek() == "}":
                self.next()
            return ("grp", inner)
        if tk == "}":
            return None
        if tk == "^":
            return ("sup", self.arg())
        if tk == "_":
            return ("sub", self.arg())
        if tk == "&":
            return ("amp",)
        if tk in ("\\\\", "\\cr"):
            return ("newline",)
        if tk.startswith("\\"):
            return self.command(tk)
        if tk.isspace():
            return ("space", 0.22)
        return ("ch", tk)

    def command(self, tk):
        name = tk[1:]
        # 手工处理 \left ... \right
        if name == "left":
            ld = self.delim_tok()
            inner = self.parse(stop=("\\right",))
            rd = None
            if self.peek() == "\\right":
                self.next()
                rd = self.delim_tok()
            return ("delim", ld, rd, inner)
        if name in ("text", "textrm", "mbox", "textbf", "textit"):
            return ("text", self.raw_arg())
        if name in ("mathrm", "operatorname", "rm"):
            return ("grp_rm", self.arg())
        if name in ("mathbf", "bf", "boldsymbol", "bm"):
            return ("grp_bf", self.arg())
        if name in ("mathit", "it"):
            return ("grp_it", self.arg())
        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            num = self.arg()
            den = self.arg()
            return ("frac", num, den)
        if name == "binom":
            return ("binom", self.arg(), self.arg())
        if name == "sqrt":
            idx = None
            if self.peek() == "[":
                self.next()
                idx = self.parse(stop=("[", "]"))
                if self.peek() == "]":
                    self.next()
            return ("sqrt", idx, self.arg())
        if name in ("hat", "widehat", "bar", "overline", "vec", "dot", "ddot", "tilde", "underline", "overbrace", "underbrace"):
            return ("accent", name, self.arg())
        if name in ("begin",):
            env = self.raw_arg().strip()
            body = self.parse(stop=("\\end",))
            if self.peek() == "\\end":
                self.next()
                self.raw_arg()
            return ("matrix", env, body)
        if name in GREEK:
            return ("ch", GREEK[name])
        if name in SYM:
            if name == "sqrt":
                return ("ch", "\u221a")
            return ("ch", SYM[name])
        if name in FUNCS:
            return ("fn", name)
        if name in BIGOPS:
            return ("bigop", name)
        if name in SPACES:
            return ("space", SPACES[name] * 1.0)
        if name in ("displaystyle", "textstyle", "limits", "nolimits", "left.", "nonumber"):
            return None
        if name in ("quad", "qquad"):
            return ("space", SPACES[name])
        if len(name) == 1:
            # \, \; \! \% \$ \# \& \_ \{ \}
            if name in SPACES:
                return ("space", SPACES[name] * 1.0)
            if name in ("(", ")"):
                return None
            return ("ch", name)
        return ("ch", name)  # 未知命令按正体文字处理，避免丢字

    def raw_arg(self):
        if self.peek() != "{":
            tk = self.next()
            return tk or ""
        self.next()
        depth = 1
        buf = []
        while True:
            tk = self.peek()
            if tk is None:
                break
            self.next()
            if tk == "{":
                depth += 1
            elif tk == "}":
                depth -= 1
                if depth == 0:
                    break
            buf.append(tk)
        return "".join(buf)

    def delim_tok(self):
        tk = self.peek()
        if tk is None:
            return None
        self.next()
        d = DELIMS.get(tk)
        if d is None and tk.startswith("\\"):
            d = SYM.get(tk[1:], tk[1:])
        return d


# --------------------------------------------------------------------------
# 排版
# --------------------------------------------------------------------------
def layout(nodes, size, style="math"):
    """节点列表 -> Box。style: math / rm / bf / it"""
    boxes = []
    i = 0
    nodes = [n for n in nodes if n is not None]
    while i < len(nodes):
        n = nodes[i]
        kind = n[0]
        nxt = nodes[i + 1] if i + 1 < len(nodes) else None

        if kind == "ch":
            s = n[1]
            kind_font = F_SERIF if style in ("rm", "bf") else F_MATH
            b = textbox(s, size, kind_font)
            boxes.append(b)
            i += 1

        elif kind in ("grp", "grp_rm", "grp_bf", "grp_it"):
            st = {"grp": style, "grp_rm": "rm", "grp_bf": "bf", "grp_it": "it"}[kind]
            boxes.append(layout(n[1], size, st))
            i += 1

        elif kind == "text":
            boxes.append(textbox(n[1], size, F_CJK if _has_cjk(n[1]) else F_SERIF))
            i += 1

        elif kind == "fn":
            boxes.append(textbox(n[1], size, F_SERIF))
            i += 1

        elif kind == "space":
            boxes.append((_mk(1, 1), max(0.0, n[1] * size), 1.0, 0.0))
            i += 1

        elif kind in ("sup", "sub"):
            base = boxes.pop() if boxes else (_mk(1, 1), 0.0, 1.0, 0.0)
            sup = sub = None
            j = i
            while j < len(nodes) and nodes[j][0] in ("sup", "sub"):
                if nodes[j][0] == "sup":
                    sup = nodes[j][1]
                else:
                    sub = nodes[j][1]
                j += 1
            boxes.append(_scripts(base, sup, sub, size))
            i = j

        elif kind == "frac":
            boxes.append(_frac(n[1], n[2], size))
            i += 1

        elif kind == "binom":
            boxes.append(_binom(n[1], n[2], size))
            i += 1

        elif kind == "sqrt":
            boxes.append(_sqrt(n[1], n[2], size))
            i += 1

        elif kind == "accent":
            boxes.append(_accent(n[1], n[2], size))
            i += 1

        elif kind == "delim":
            inner = layout(n[3], size, style)
            boxes.append(_delim(n[1], n[2], inner, size))
            i += 1

        elif kind == "bigop":
            name = n[1]
            # 紧跟的 ^ _ 作为上下限
            sup = sub = None
            j = i + 1
            while j < len(nodes) and nodes[j][0] in ("sup", "sub"):
                if nodes[j][0] == "sup":
                    sup = nodes[j][1]
                else:
                    sub = nodes[j][1]
                j += 1
            boxes.append(_bigop(name, sup, sub, size))
            i = j

        elif kind == "matrix":
            boxes.append(_matrix(n[1], n[2], size))
            i += 1

        elif kind == "newline":
            boxes.append((_mk(1, 1), 0.0, size * 0.6, size * 0.45))
            i += 1

        else:
            i += 1
    return hcat(boxes)


def _has_cjk(s):
    return any("\u4e00" <= c <= "\u9fff" for c in s)


def _scripts(base, sup, sub, size):
    ss = max(size * 0.72, 8.0)
    parts = [base]
    if sup is not None:
        sb = shift(layout(sup, ss), size * 0.44)
        parts.append(sb)
    if sub is not None:
        db = shift(layout(sub, ss), -size * 0.20)
        parts.append(db)
    return hcat(parts)


def _frac(num, den, size):
    sub = max(size * 0.85, 8.5)          # 嵌套分式别缩到看不清
    nb = layout(num, sub)
    db = layout(den, sub)
    W = max(nb[1], db[1]) + size * 0.10
    gap = size * 0.15
    lw = max(2, int(round(size * 0.065)))
    axis = size * 0.28                     # 分数线在基线上方
    H = nb[2] + gap + lw + gap + db[2]
    img = _mk(W, H)
    img.paste(nb[0], (int((W - nb[1]) / 2), 0), nb[0])
    y = nb[2] + gap
    _PILDraw.Draw(img).rectangle([0, y, W - 1, y + lw - 1], fill=255)
    img.paste(db[0], (int((W - db[1]) / 2), int(y + lw + gap)), db[0])
    return (img, W, H, y + lw / 2 + axis)


def _binom(a, b, size):
    inner = _frac(a, b, size * 1.02)
    return _delim("(", ")", inner, size, pad_k=0.12)


def _sqrt(idx, body, size):
    inner = layout(body, size)
    if idx:
        ib = layout(idx, size * 0.5)
        ib = shift(ib, inner[2] * 0.55)
    else:
        ib = (_mk(1, 1), 0.0, 0.0, 0.0)
    h = inner[2]
    t = max(2, int(round(size * 0.062)))
    ww = size * 0.62
    img = _mk(ww + inner[1] + size * 0.12 + ib[1], h + t + size * 0.06)
    d = _PILDraw.Draw(img)
    y0 = t + size * 0.02
    xoff = ib[1]
    pts = [(xoff + ww * 0.02, y0 + h * 0.62),
           (xoff + ww * 0.26, y0 + h * 0.52),
           (xoff + ww * 0.44, y0 + h * 0.98),
           (xoff + ww * 0.98, y0 + t * 0.5)]
    d.line(pts, fill=255, width=t, joint="curve")
    d.line([(xoff + ww * 0.95, y0 + t * 0.5), (img.size[0] - 1, y0 + t * 0.5)],
           fill=255, width=t)
    img.paste(inner[0], (int(xoff + ww + size * 0.10), int(y0 + t * 0.7)), inner[0])
    if idx:
        img.paste(ib[0], (0, int(y0 + h * 0.15 - ib[3])), ib[0])
    asc = t + size * 0.06 + inner[3]
    return (img, float(img.size[0]), float(img.size[1]), asc)


def _accent(kind, body, size):
    b = layout(body, size)
    t = max(1, int(round(size * 0.055)))
    if kind in ("bar", "overline"):
        return hcat([b]) if False else _overline(b, t, size)
    if kind == "underline":
        img, w, h, asc = b
        out = _mk(w, h + size * 0.16)
        out.paste(img, (0, 0), img)
        d = _PILDraw.Draw(out)
        y = min(out.size[1] - 1, int(asc + size * 0.10))
        d.rectangle([0, y, w - 1, y + t - 1], fill=255)
        return (out, w, out.size[1], asc)
    if kind in ("hat", "widehat"):
        return _hat(b, size, "hat")
    if kind == "tilde":
        return _hat(b, size, "tilde")
    if kind == "vec":
        return _hat(b, size, "vec")
    if kind in ("dot", "ddot"):
        return _hat(b, size, kind)
    if kind in ("overbrace", "underbrace"):
        return _over(b, "\u23de", size, scale=max(1.0, b[1] / size))
    return b


def _overline(b, t, size):
    img, w, h, asc = b
    out = _mk(w, h + t + size * 0.06)
    out.paste(img, (0, int(t + size * 0.06)), img)
    d = _PILDraw.Draw(out)
    d.rectangle([0, 0, w - 1, t - 1], fill=255)
    return (out, w, out.size[1], asc + t + size * 0.06)


def _hat(b, size, kind):
    img, w, h, asc = b
    t = max(1, int(round(size * 0.055)))
    hh = size * 0.34
    H = h + hh
    out = _mk(w, H)
    out.paste(img, (0, int(hh)), img)
    d = _PILDraw.Draw(out)
    mx = w * 0.5
    mw = min(w * 0.62, size * 0.85)
    if kind in ("hat", "widehat"):
        d.line([(mx - mw / 2, hh * 0.85), (mx, hh * 0.12), (mx + mw / 2, hh * 0.85)],
               fill=255, width=t, joint="curve")
    elif kind == "tilde":
        d.arc([mx - mw / 2, hh * 0.10, mx + mw / 2, hh * 1.15], start=200, end=340,
              fill=255, width=t)
    elif kind == "vec":
        y = hh * 0.45
        d.line([(mx - mw / 2, y), (mx + mw / 2, y)], fill=255, width=t)
        d.line([(mx + mw / 2 - t * 2.2, y - t * 1.8), (mx + mw / 2, y),
                (mx + mw / 2 - t * 2.2, y + t * 1.8)], fill=255, width=t, joint="curve")
    else:  # dot / ddot
        r = max(1.0, size * 0.075)
        xs = [mx] if kind == "dot" else [mx - r * 1.6, mx + r * 1.6]
        for x in xs:
            d.ellipse([x - r, hh * 0.20 - r, x + r, hh * 0.20 + r], fill=255)
    return (out, w, H, asc + hh)


def _over(b, mark, size, scale=1.0, dy=0.0):
    m = textbox(mark, size * 0.9 * scale, F_MATH)
    m = scale_box(m, 1.0)
    img, w, h, asc = b
    mw = m[1]
    W = max(w, mw)
    H = h + m[2] * 1.02
    out = _mk(W, H)
    out.paste(img, (int((W - w) / 2), int(m[2] * 1.02 + dy * size)), img)
    out.paste(m[0], (int((W - mw) / 2), 0), m[0])
    return (out, W, H, asc + m[2] * 1.02)


def _delim(ld, rd, inner, size, pad_k=0.0):
    parts = []
    pw = max(1, int(round(size * 0.05)))
    if ld is not None:
        parts.append(_delim_glyph(ld, inner[2], inner[3], size, True, pad_k))
    parts.append(inner)
    if rd is not None:
        parts.append(_delim_glyph(rd, inner[2], inner[3], size, False, pad_k))
    return hcat(parts)


def _delim_glyph(ch, h, asc, size, left, pad_k=0.0):
    need = h + size * (0.12 + pad_k * 2)
    base = textbox(ch, size, F_MATH)
    if need <= base[2] * 1.06 and ch not in ("{", "}"):
        return shift(base, asc - base[3])
    t = max(2, int(round(size * 0.062)))
    if ch in ("(", ")"):
        arc_w = size * 0.30
        w = arc_w + size * 0.10
        img = _mk(w, need)
        d = _PILDraw.Draw(img)
        x0 = size * 0.03 if left else (w - arc_w * 2 - size * 0.03)
        bbox = [x0, -need * 0.01, x0 + arc_w * 2, need * 1.01]
        if left:
            d.arc(bbox, start=90, end=270, fill=255, width=t)
        else:
            d.arc(bbox, start=270, end=90, fill=255, width=t)
    elif ch in ("[", "]"):
        w = size * 0.34
        img = _mk(w, need)
        d = _PILDraw.Draw(img)
        xe = size * 0.06 if left else w - size * 0.06
        xo = xe + (size * 0.16 if left else -size * 0.16)
        d.line([(xo, t / 2), (xe, t / 2), (xe, need - t / 2), (xo, need - t / 2)],
               fill=255, width=t, joint="curve")
    elif ch in ("\u27e8", "\u27e9"):
        w = size * 0.42
        img = _mk(w, need)
        d = _PILDraw.Draw(img)
        if left:
            d.line([(w - t, t / 2), (t / 2, need / 2), (w - t, need - t / 2)],
                   fill=255, width=t, joint="curve")
        else:
            d.line([(t / 2, t / 2), (w - t, need / 2), (t / 2, need - t / 2)],
                   fill=255, width=t, joint="curve")
    elif ch in ("{", "}"):
        w = size * 0.40
        img = _mk(w, need)
        d = _PILDraw.Draw(img)
        xin = size * 0.05 if left else w - size * 0.05
        xout = size * 0.30 if left else w - size * 0.30
        mid = need / 2
        r = size * 0.16
        # 上臂
        d.arc([xout - r, t / 2 - r, xout + r, t / 2 + r], start=0, end=180, fill=255, width=t)
        d.line([(xout, t / 2), (xin, t / 2)], fill=255, width=t)
        d.line([(xin, t / 2), (xin, mid - r)], fill=255, width=t)
        d.arc([xin - r if left else xin - r, mid - r, xin + r, mid + r],
              start=270 if left else 90, end=90 if left else 270, fill=255, width=t)
        d.line([(xin, mid + r), (xin, need - t / 2)], fill=255, width=t)
        d.line([(xin, need - t / 2), (xout, need - t / 2)], fill=255, width=t)
        d.arc([xout - r, need - t / 2 - r, xout + r, need - t / 2 + r],
              start=180, end=360, fill=255, width=t)
    elif ch in ("|", "\u2016"):
        w = size * 0.20
        img = _mk(w, need)
        _PILDraw.Draw(img).rectangle([w * 0.35, 0, w * 0.35 + t, need - 1], fill=255)
    else:
        return shift(base, asc - base[3])
    return (img, float(img.size[0]), float(img.size[1]), asc + size * (0.06 + pad_k))


def _bigop(name, sup, sub, size):
    sym = SYM.get(name, name)
    bsz = size * (1.35 if name in INTEGRALS else 1.5)
    b = textbox(sym, bsz, F_MATH)
    if sup is None and sub is None:
        return b
    lim_sz = size * 0.7
    if name in INTEGRALS:
        # 积分：上下限放右侧
        parts = [b]
        if sup:
            sb = layout(sup, lim_sz)
            parts.append(shift(sb, b[2] * 0.45))
        if sub:
            db = layout(sub, lim_sz)
            parts.append(shift(db, -b[2] * 0.28))
        return hcat(parts)
    col = []
    if sup:
        col.append(layout(sup, lim_sz))
    col.append(b)
    if sub:
        col.append(layout(sub, lim_sz))
    return vcat(col, gap=size * 0.04)


def _matrix(env, body, size):
    rows = [[]]
    cur = []
    for n in body:
        if n is None:
            continue
        if n[0] == "newline":
            cur.append(n)
            rows[-1] = cur
            cur = []
            rows.append([])
            continue
        cur.append(n)
    if cur:
        rows[-1] = cur
    rows = [r for r in rows if any(x[0] != "space" for x in r) or r]

    cells = []
    for r in rows:
        cols, cell = [], []
        for n in r:
            if n[0] == "space" and getattr(n, "_amp", False):
                pass
            cell.append(n)
        cells.append(cell)
    # 用 & 切列：解析时 & -> ('space', 0.0) 丢了标记，这里改用文本 & 的节点
    # 简化：重新按 ('amp',) 切（parse_one 里 & 已映射为 amp 见下）
    grid = []
    for r in rows:
        row, cell = [], []
        for n in r:
            if n[0] == "amp":
                row.append(cell)
                cell = []
            else:
                cell.append(n)
        row.append(cell)
        grid.append(row)

    msz = size * 0.95
    laid = [[layout(c, msz) for c in row] for row in grid]
    ncol = max(len(r) for r in laid)
    colw = [0.0] * ncol
    for row in laid:
        for j, b in enumerate(row):
            colw[j] = max(colw[j], b[1])
    cg = size * 0.55
    rg = size * 0.32
    rowh = [max(b[2] for b in row) for row in laid]
    asc0 = [max(b[3] for b in row) for row in laid]
    W = sum(colw) + cg * (ncol - 1)
    H = sum(rowh) + rg * (len(rowh) - 1)
    img = _mk(W, H)
    y = 0.0
    for ri, row in enumerate(laid):
        x = 0.0
        for j, b in enumerate(row):
            dx = x + (colw[j] - b[1]) / 2
            img.paste(b[0], (int(dx), int(y + asc0[ri] - b[3])), b[0])
            x += colw[j] + cg
        y += rowh[ri] + rg
    box = (img, W, H, asc0[0] if asc0 else H / 2)
    pre = {"pmatrix": ("(", ")"), "bmatrix": ("[", "]"),
           "vmatrix": ("|", "|"), "Bmatrix": ("{", "}"),
           "cases": ("{", None)}.get(env)
    if pre:
        return _delim(pre[0], pre[1], box, size, pad_k=0.12)
    return box


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------
def render(tex, size=17, color=(235, 235, 235)):
    """tex: 纯公式源码（不带 $）。返回 (PIL RGBA Image, asc_px) 或 None。"""
    try:
        p = P(tokenize(tex))
        nodes = p.parse()
        box = layout(nodes, size * SS)
        img, w, h, asc = box
        if img.size[0] < 2 or img.size[1] < 2:
            return None
        if w > 4000 * SS:
            return None
        img = img.resize((max(1, int(round(w / SS))), max(1, int(round(h / SS)))),
                         _PILImage.LANCZOS)
        out = _PILImage.new("RGBA", img.size, (0, 0, 0, 0))
        solid = _PILImage.new("RGBA", img.size, tuple(color) + (255,))
        out.paste(solid, (0, 0), img)
        return out, asc / SS
    except Exception:
        return None


MATH_SPLIT = re.compile(r"(\$\$.*?\$\$|\$[^$\n]+?\$|\\\[.*?\\\]|\\\(.*?\\\))", re.S)
FENCE_SPLIT = re.compile(r"(```.*?```)", re.S)
ESC_DOLLAR = "\x01"      # 转义美元 \$ 的临时占位


def _split_inline(s):
    out = []
    for part in MATH_SPLIT.split(s):
        if not part:
            continue
        if part.startswith("$$") and part.endswith("$$") and len(part) > 3:
            out.append(("math", part[2:-2], True))
        elif part.startswith("\\[") and part.endswith("\\]"):
            out.append(("math", part[2:-2], True))
        elif part.startswith("$") and part.endswith("$") and len(part) > 2:
            out.append(("math", part[1:-1], False))
        elif part.startswith("\\(") and part.endswith("\\)"):
            out.append(("math", part[2:-2], False))
        else:
            out.append(("text", part))
    return out


def split_math(s):
    """-> [('text', s) | ('math', src, displaybool)]

    代码块里的 $ 不当公式；\$（金额）也不算公式分隔符。
    """
    out = []
    for i, chunk in enumerate(FENCE_SPLIT.split(s)):
        if not chunk:
            continue
        if i % 2:                       # ```...``` 原样保留，里面的 ** 不当作粗体
            out.append(("code", chunk))
            continue
        for seg in _split_inline(chunk.replace("\\$", ESC_DOLLAR)):
            if seg[0] == "text":
                out.append(("text", seg[1].replace(ESC_DOLLAR, "$")))
            else:
                out.append(("math", seg[1].replace(ESC_DOLLAR, "$"), seg[2]))
    return out


_SUP = {"0": "\u2070", "1": "\u00b9", "2": "\u00b2", "3": "\u00b3", "4": "\u2074",
        "5": "\u2075", "6": "\u2076", "7": "\u2077", "8": "\u2078", "9": "\u2079",
        "+": "\u207a", "-": "\u207b", "n": "\u207f", "i": "\u2071"}
_SUB = {"0": "\u2080", "1": "\u2081", "2": "\u2082", "3": "\u2083", "4": "\u2084",
        "5": "\u2085", "6": "\u2086", "7": "\u2087", "8": "\u2088", "9": "\u2089",
        "+": "\u208a", "-": "\u208b", "n": "\u2099", "i": "\u1d62", "j": "\u2c7c"}

_FRAC_SIMPLE = re.compile(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_SQRT_SIMPLE = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
_TEXT_CMD = re.compile(r"\\(?:text|mathrm|textrm|mathbf|mathit|operatorname)\s*\{([^{}]*)\}")
_SUP_CMD = re.compile(r"\^\s*\{?([0-9n+\-])\}?")
_SUB_CMD = re.compile(r"_\s*\{?([0-9n+\-])\}?")
_CMD = re.compile(r"\\([a-zA-Z]+)")


def latex_to_unicode(s):
    """流式阶段的近似显示：把公式源码变可读文本，不是终稿。"""
    for tok in ("$$", "\\[", "\\]", "\\(", "\\)"):
        s = s.replace(tok, "")
    for _ in range(3):
        s = _FRAC_SIMPLE.sub(lambda m: "(%s)/(%s)" % (m.group(1), m.group(2)), s)
        s = _SQRT_SIMPLE.sub(lambda m: "\u221a(%s)" % m.group(1), s)
    s = _TEXT_CMD.sub(lambda m: m.group(1), s)
    s = _SUP_CMD.sub(lambda m: _SUP.get(m.group(1), m.group(0)), s)
    s = _SUB_CMD.sub(lambda m: _SUB.get(m.group(1), m.group(0)), s)

    def _rep(m):
        n = m.group(1)
        return GREEK.get(n) or SYM.get(n) or m.group(0)   # 不认识的命令留着，别把 C:\Users 吃成 C:Users
    s = _CMD.sub(_rep, s)
    return s


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
        self.tok_in = 0          # 累计精确值（API usage）
        self.tok_out = 0
        self.cost_usd = 0.0      # 累计花费（按官方分档价估算）
        self.cost_known = True   # 遇到未定价模型就置 False，显示 ¥—
        self._tok_pending = 0    # 工作线程写入的已收字符数
        self._tok_shown = -1
        self._tok_live = False
        self.photo = None
        self.thumb_img = None
        self._photos: list = []   # 公式图片引用（不放这里会被 GC 掉）
        self._ai_start = None     # 本轮回复在 text 里的起点，流式结束后重绘用
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
        self.text.tag_configure("bold", font=(SANS, 12, "bold"))
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

        status_row = tk.Frame(foot, bg=PANEL)
        status_row.pack(side="bottom", fill="x", padx=18, pady=(0, 10))
        tk.Label(status_row, text="ENTER SEND    ESC HIDE    ^R RECAP", bg=PANEL, fg=MUTED,
                 font=(PIX, PIX_SZ)).pack(side="left")
        self.tok_lbl = tk.Label(status_row, text="TOK  --", bg=PANEL, fg=LINE2,
                                font=(PIX, PIX_SZ))
        self.tok_lbl.pack(side="right")

        win.bind("<Escape>", lambda e: (self._close_panel() if self.panel else self.hide()))
        win.bind("<Control-r>", lambda e: self.resnap())
        win.bind("<Control-q>", lambda e: self.hide())

        if not DS_KEY and prov()["api"] != "ollama":
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
        for key, pdef in PROVIDERS.items():
            holder = tk.Frame(tabs, bg=LINE2)
            holder.pack(side="left", padx=(0, 6))
            lb = tk.Label(holder, text=pdef["label"], bg=PANEL, fg=DIM, font=(PIX, PIX_SZ),
                          padx=10, pady=6, cursor="hand2")
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

        self.f_model = field("MODEL", DS_MODEL)
        self.f_url = field("BASE URL", DS_BASE)
        self.f_key = field("API KEY", DS_KEY, show="•")
        self._pick_provider(PROVIDER)

    def _pick_provider(self, k):
        self.pv.set(k)
        for key, lb in self.tab_map.items():
            lb.configure(bg=ACCENT if key == k else PANEL, fg="#0b0b0d" if key == k else DIM)
        pdef = prov(k)
        if k == PROVIDER:
            model, url, keyv = DS_MODEL, DS_BASE, DS_KEY
        else:
            model, url, keyv = pdef["model"], pdef["base"], key_for(k)
        local = pdef["api"] == "ollama"
        for w, val in ((self.f_model, model), (self.f_url, url)):
            w.configure(state="normal")
            w.delete(0, "end")
            w.insert(0, val or "")
        self.f_key.configure(state="normal")
        self.f_key.delete(0, "end")
        if local:
            self.f_key.configure(state="disabled")
        else:
            self.f_key.insert(0, keyv or "")

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
        pdef = prov(k)
        if not model:
            self.pmsg.configure(text="MODEL 不能为空", fg=ACCENT)
            return
        if not (url or pdef["base"]):
            self.pmsg.configure(text="BASE URL 不能为空", fg=ACCENT)
            return
        if pdef["api"] != "ollama" and not key:
            self.pmsg.configure(text="API KEY 不能为空", fg=ACCENT)
            return
        upd = {"SA_PROVIDER": k, "SA_MODEL": model, "SA_BASE": url or pdef["base"]}
        if pdef["api"] == "ollama":
            OLLAMA_MODEL = model
            OLLAMA_BASE = url or pdef["base"]
            upd["SA_OLLAMA_MODEL"] = OLLAMA_MODEL
            upd["OLLAMA_URL"] = OLLAMA_BASE
        else:
            upd["SA_KEY"] = key
            if k in _KEY_ENV:
                upd[_KEY_ENV[k]] = key
        try:
            write_env(upd)
        except Exception as e:
            self.pmsg.configure(text="保存失败：%s" % e, fg=ACCENT)
            return

        PROVIDER = k
        DS_MODEL = model
        DS_BASE = url or pdef["base"]
        if key:
            DS_KEY = key
        MODEL_LABEL = model
        self.cost_known = True
        self.model_lbl.configure(text=self._model_text())
        self.messages = []
        self._close_panel()
        self.clear_chat()
        self._insert("CONFIG APPLIED\n", "meta")
        self._insert("%s · %s\n" % (PROVIDER, MODEL_LABEL), "meta")
        self.set_status("APPLIED", ACCENT)
        log("config applied provider=%s api=%s model=%s"
            % (PROVIDER, prov()["api"], MODEL_LABEL))
        if prov()["api"] == "ollama":
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
                and self.win.winfo_viewable() and not self.busy
                and getattr(self, "viewer", None) is None):
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
        self._photos.clear()
        self._ai_start = None
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
        self._tok_pending = 0
        self._tok_shown = -1
        self._tok_live = True
        self.cost_known = True
        self._tok_show()
        self.send_btn.configure(bg=PANEL, fg=DIM, text="WAIT")
        self.set_status("STREAMING", ACCENT)

        self.text.configure(state="normal")
        self.text.insert("end", "YOU\n", "you")
        self.text.insert("end", q + "\n", "body")
        self.text.insert("end", (MODEL_LABEL.split(":")[0].upper() if PROVIDER == "ollama" else "MODEL") + "\n",
                         "ai")
        self.text.configure(state="disabled")
        self._ai_start = self.text.index("end-1c")   # 流式结束后从这里整体重绘
        self.text.see("end")

        if not self.messages:
            user_msg = {"role": "user", "content": q, "images": [self.image_b64]}
        else:
            user_msg = {"role": "user", "content": q}
        self.messages.append(user_msg)
        self.t0 = time.time()
        threading.Thread(target=self._worker, args=(list(self.messages),), daemon=True).start()

    @staticmethod
    def _to_anthropic(msgs):
        """转 Anthropic Messages：图片走 base64 source，角色必须交替。"""
        out = []
        for m in msgs:
            role = m.get("role") or "user"
            imgs = m.get("images") or []
            if imgs:
                content = [{"type": "image",
                            "source": {"type": "base64", "media_type": "image/jpeg",
                                       "data": b}} for b in imgs]
                content.append({"type": "text", "text": m.get("content") or ""})
                out.append({"role": role, "content": content})
            else:
                txt = m.get("content") or ""
                if out and out[-1]["role"] == role and isinstance(out[-1]["content"], str):
                    out[-1]["content"] += "\n" + txt
                else:
                    out.append({"role": role, "content": txt})
        return out

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
            api_kind = prov()["api"]
            if api_kind == "ollama":
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
                            self._tok_pending = len(acc)
                            self.ui_q.put(("delta", d))
                        if j.get("done"):
                            self.ui_q.put(("usage", (j.get("prompt_eval_count") or 0,
                                                     j.get("eval_count") or 0, 0)))
                            break
            elif api_kind == "anthropic":
                if not DS_KEY:
                    raise RuntimeError("缺少 API KEY —— 点顶栏 API 填写")
                payload = {
                    "model": DS_MODEL,
                    "messages": self._to_anthropic(msgs),
                    "max_tokens": 3000,
                    "stream": True,
                }
                req = urllib.request.Request(
                    endpoint("/v1/messages"),
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json",
                             "x-api-key": DS_KEY,
                             "anthropic-version": "2023-06-01"},
                )
                t_in = 0
                t_hit = 0
                with urllib.request.urlopen(req, timeout=300) as r:
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
                        et = j.get("type")
                        if et == "content_block_delta":
                            dl = (j.get("delta") or {}).get("text")
                            if dl:
                                acc += dl
                                self._tok_pending = len(acc)
                                self.ui_q.put(("delta", dl))
                        elif et == "message_start":
                            u = (j.get("message") or {}).get("usage") or {}
                            t_hit = u.get("cache_read_input_tokens") or 0
                            t_in = (u.get("input_tokens") or 0) + t_hit
                        elif et == "message_delta":
                            u = j.get("usage") or {}
                            if u.get("output_tokens") is not None:
                                self.ui_q.put(("usage", (t_in, u.get("output_tokens") or 0, t_hit)))
                        elif et == "error":
                            er = (j.get("error") or {}).get("message") or "anthropic error"
                            raise RuntimeError(er)
            else:
                if not DS_KEY:
                    raise RuntimeError("缺少 API KEY —— 点顶栏 API 填写")
                payload = {
                    "model": DS_MODEL,
                    "messages": self._to_openai(msgs),
                    "stream": True,
                    "max_tokens": 3000,
                }
                if PROVIDER == "deepseek":
                    payload["thinking"] = {"type": "disabled"}
                if PROVIDER in ("deepseek", "openai", "openrouter"):
                    payload["stream_options"] = {"include_usage": True}
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
                            self._tok_pending = len(acc)
                            self.ui_q.put(("delta", delta))
                        u = j.get("usage")
                        if u:
                            ch_hit = (u.get("prompt_cache_hit_tokens")
                                      or (u.get("prompt_tokens_details") or {}).get("cached_tokens")
                                      or 0)
                            self.ui_q.put(("usage", (u.get("prompt_tokens") or 0,
                                                     u.get("completion_tokens") or 0, ch_hit)))
            self.ui_q.put(("done", acc))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            self.ui_q.put(("err", "HTTP %s %s" % (e.code, body)))
        except Exception as e:
            self.ui_q.put(("err", "%s: %s" % (type(e).__name__, e)))

    @staticmethod
    def _fmt_tok(n):
        n = int(n or 0)
        if n >= 10000:
            return "%.0fk" % (n / 1000.0)
        if n >= 1000:
            return "%.1fk" % (n / 1000.0)
        return str(n)

    @staticmethod
    def _fmt_cny(usd):
        c = (usd or 0.0) * USD_CNY
        if c <= 0:
            return "\u00a50"
        if c < 0.0001:
            return "\u00a5<0.0001"
        if c < 0.01:
            return "\u00a5%.4f" % c
        if c < 1:
            return "\u00a5%.3f" % c
        return "\u00a5%.2f" % c

    def _tok_show(self, inn=None, out=None):
        """流式只报输出（不编造输入数），结束后补 API 返回的精确 usage。"""
        if getattr(self, "tok_lbl", None) is None:
            return
        if self._tok_live:
            est = int(self._tok_pending / 1.8) if self._tok_pending else 0
            self.tok_lbl.configure(text="TOK  \u2193%s" % self._fmt_tok(est), fg=ACCENT)
            return
        if inn is None:
            inn, out = self.tok_in, self.tok_out
        self.tok_lbl.configure(
            text="TOK  \u2191%s  \u2193%s  \u03a3%s \u00b7 %s"
                 % (self._fmt_tok(inn), self._fmt_tok(out),
                    self._fmt_tok(self.tok_in + self.tok_out),
                    self._fmt_cny(self.cost_usd) if self.cost_known else "\u00a5\u2014"),
            fg=MUTED)

    def _insert(self, s, tag):
        self.text.configure(state="normal")
        self.text.insert("end", s, tag)
        self.text.configure(state="disabled")
        self.text.see("end")

    # ---------------- 公式渲染 ----------------
    def _math_px(self) -> int:
        """行内公式字号：跟正文行高对齐。"""
        try:
            f = tkfont.Font(root=self.root, font=(SANS, 12))
            return max(12, int(round(f.metrics("linespace") * 0.80)))
        except Exception:
            return 15

    def _img_math(self, src, tag="body") -> bool:
        if F_MATH is None:
            return False
        try:
            rgb = tuple(int(INK[i:i + 2], 16) for i in (1, 3, 5))
            r = render(src, self._math_px(), rgb)
        except Exception:
            log("latex render fail: %s" % traceback.format_exc(limit=1))
            r = None
        if not r:
            return False
        im = r[0]
        try:
            maxw = max(160, self.text.winfo_width() - 64)
            if im.size[0] > maxw:
                k = maxw / float(im.size[0])
                im = im.resize((int(maxw), max(1, int(im.size[1] * k))), _PILImage.LANCZOS)
            flat = _PILImage.new("RGB", im.size, self.text.cget("bg"))
            flat.paste(im, (0, 0), im)
            ph = ImageTk.PhotoImage(flat)
        except Exception:
            return False
        self._photos.append(ph)
        self.text.image_create("end", image=ph, align="center", padx=3, pady=0)
        return True

    def _emit_text(self, t, tag="body"):
        """正文里 **粗体** 变真粗体。

        用状态机而不是正则：\`\`**加粗含 $x^2$ 公式**\`\` 会被 split_math 切成几段，
        正则各段匹配不上，状态机能跨公式继续加粗。
        """
        parts = t.split("**")
        for i, part in enumerate(parts):
            if part:
                self.text.insert("end", part, (tag, "bold") if self._bold_on else tag)
            if i < len(parts) - 1:
                self._bold_on = not self._bold_on

    def _emit(self, src, tag="body"):
        """按 $...$ / $$...$$ 切段：文字直插，公式渲染成图片。"""
        prev_disp = False
        self._bold_on = False
        for seg in split_math(src):
            if seg[0] == "code":
                prev_disp = False
                self.text.insert("end", seg[1], tag)
                continue
            if seg[0] == "text":
                t = seg[1]
                if prev_disp and t.startswith("\n"):
                    t = t[1:]        # 行间公式后面已经补过换行，别再多空一行
                prev_disp = False
                if t:
                    self._emit_text(t, tag)
                continue
            body, disp = seg[1], seg[2]
            tex = body.strip()
            if not tex:
                continue
            if disp:
                tail = self.text.get("end-3c", "end-1c")
                if not tail.endswith("\n\n"):
                    self.text.insert("end", "\n", tag)
            if not self._img_math(tex, tag):
                self.text.insert("end", body, tag)   # 渲染失败就显示源码，不吞内容
            if disp:
                self.text.insert("end", "\n", tag)
                prev_disp = True

    def _replace_stream(self, src, tag="body"):
        """把流式期间的草稿整段替换成渲染版（公式变图）。"""
        start = getattr(self, "_ai_start", None)
        self.text.configure(state="normal")
        if start:
            try:
                self.text.delete(start, "end-1c")
            except tk.TclError:
                pass
        self._emit(src, tag)
        self.text.insert("end", "\n", tag)
        self.text.configure(state="disabled")
        self.text.see("end")

    def poll(self):
        if self._tok_live and self._tok_pending != self._tok_shown:
            self._tok_shown = self._tok_pending
            self._tok_show()
        try:
            while True:
                kind, payload = self.ui_q.get_nowait()
                if kind == "delta":
                    self._insert(latex_to_unicode(payload), "body")
                elif kind == "usage":
                    self.tok_in += payload[0]
                    self.tok_out += payload[1]
                    _c = token_cost(payload[0], payload[1],
                                    payload[2] if len(payload) > 2 else 0)
                    self.cost_usd += _c or 0.0
                    self.cost_known = self.cost_known and (_c is not None)
                    self._tok_live = False
                    self._tok_show(payload[0], payload[1])
                elif kind == "done":
                    self.messages.append({"role": "assistant", "content": payload})
                    self.busy = False
                    self.send_btn.configure(bg=PANEL, fg=ACCENT, text="SEND")
                    el = time.time() - self.t0
                    if not payload:
                        self._replace_stream("(空回复)\n", "err")
                    else:
                        self._replace_stream(payload, "body")
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
