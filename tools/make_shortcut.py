"""给 Screen Ask 建桌面快捷方式（Windows / macOS / Linux）。

用法：
    python tools/make_shortcut.py            # 建到桌面
    python tools/make_shortcut.py --remove   # 删掉

Windows 用 WScript.Shell 写 .lnk（不依赖 pywin32），图标用 assets/icon.ico。
"""
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "screen_ask.py"
ICON = ROOT / "assets" / "icon.ico"
NAME = "Screen Ask"


def _desktop() -> Path:
    for cand in (Path.home() / "Desktop", Path.home() / "桌面"):
        if cand.is_dir():
            return cand
    return Path.home()


def _pythonw() -> str:
    """优先 pythonw.exe：不弹控制台窗口。"""
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.exists() else exe)


def install_windows() -> int:
    lnk = _desktop() / ("%s.lnk" % NAME)
    cmd = [
        "powershell", "-NoProfile", "-Command",
        "$W=New-Object -ComObject WScript.Shell;"
        "$S=$W.CreateShortcut('%s');"
        "$S.TargetPath='%s';"
        "$S.Arguments='\"%s\"';"
        "$S.WorkingDirectory='%s';"
        "$S.IconLocation='%s';"
        "$S.Description='%s';"
        "$S.Save()"
        % (str(lnk), _pythonw(), str(ENTRY), str(ROOT),
           str(ICON) if ICON.exists() else str(ENTRY), NAME),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("失败:", (r.stderr or r.stdout).strip()[:400])
        return 1
    print("已创建:", lnk, "| 图标:", ICON.name if ICON.exists() else "默认")
    return 0


def install_macos() -> int:
    p = _desktop() / ("%s.command" % NAME)
    p.write_text(
        "#!/bin/sh\ncd '%s'\nexec '%s' '%s'\n"
        % (ROOT, sys.executable, ENTRY), encoding="utf-8")
    p.chmod(0o755)
    print("已创建:", p)
    return 0


def install_linux() -> int:
    apps = Path.home() / ".local" / "share" / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    body = (
        "[Desktop Entry]\nType=Application\nName=%s\n"
        "Exec=%s %s\nPath=%s\nIcon=%s\nTerminal=false\nCategories=Utility;\n"
        % (NAME, sys.executable, ENTRY, ROOT,
           ICON if ICON.exists() else "utilities-terminal")
    )
    desk = _desktop() / ("%s.desktop" % NAME)
    for target in (apps / "screen-ask.desktop", desk):
        target.write_text(body, encoding="utf-8")
        target.chmod(0o755)
    print("已创建:", apps / "screen-ask.desktop")
    return 0


def remove() -> int:
    sysname = platform.system()
    targets = []
    if sysname == "Windows":
        targets = [_desktop() / ("%s.lnk" % NAME)]
    elif sysname == "Darwin":
        targets = [_desktop() / ("%s.command" % NAME)]
    else:
        targets = [Path.home() / ".local" / "share" / "applications" / "screen-ask.desktop",
                   _desktop() / ("%s.desktop" % NAME)]
    for t in targets:
        if t.exists():
            t.unlink()
            print("已删除:", t)
    return 0


def main() -> int:
    if not ENTRY.exists():
        print("找不到 screen_ask.py —— 请在项目根目录运行")
        return 1
    if "--remove" in sys.argv:
        return remove()
    sysname = platform.system()
    if sysname == "Windows":
        return install_windows()
    if sysname == "Darwin":
        return install_macos()
    return install_linux()


if __name__ == "__main__":
    sys.exit(main())
