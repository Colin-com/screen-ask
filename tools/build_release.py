"""把仓库打成可分发的 zip（Release 附件）。

排除：.git、.env、虚拟环境、测试截图、缓存。保留 assets/icon.ico（如果有）。
用法：python tools/build_release.py  ->  dist/screen-ask-<version>.zip
"""
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".github", "__pycache__", "dist", "venv", ".venv", "node_modules"}
SKIP_FILES = {".env", "window.png", "boot_2.png", "final.png", "fsize.png",
              "ui_8bit.png", "boot_8bit.png", "tok_zoom.png", "panel_full.png",
              "panel_right.png", "icon_preview.png", "icon_16.png", "icon_32.png",
              ".DS_Store"}
SKIP_SUFFIX = (".pyc", ".pyo", ".log")


def version() -> str:
    txt = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', txt, re.M)
    return m.group(1) if m else "0.0.0"


def build() -> Path:
    ver = version()
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    out = dist / ("screen-ask-%s.zip" % ver)
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(ROOT.rglob("*")):
            rel = p.relative_to(ROOT)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            if not p.is_file():
                continue
            if p.name in SKIP_FILES or p.name.endswith(SKIP_SUFFIX):
                continue
            # 根目录下的测试截图整体跳过（docs/ 里的保留）
            if p.suffix.lower() == ".png" and len(rel.parts) == 1:
                continue
            z.write(p, Path("screen-ask") / rel)
            count += 1
    print("wrote %s  (%d files, %.1f KB)" % (out, count, out.stat().st_size / 1024))
    return out


if __name__ == "__main__":
    sys.exit(0 if build() else 1)
