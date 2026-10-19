"""生成 8-bit 像素图标（多尺寸 .ico + 预览 png）。

设计：32x32 逻辑网格，先按格画硬边，再整数倍放大 —— 不用任何抗锯齿，
所以 256px 下每个"像素"是 8x8 的实心方块，缩到 16px 也还是硬边。
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
BG = (11, 11, 13)
RED = (255, 45, 45)
GRID = 32


def build() -> Image.Image:
    im = Image.new("RGB", (GRID, GRID), BG)
    d = ImageDraw.Draw(im)

    # 线宽统一 2 格：角标 2 格粗、准星 2 格宽，避免"四角重、中心轻"
    thick, arm, pad = 2, 10, 1
    corners = ((pad, pad, 1, 1), (GRID - 1 - pad, pad, -1, 1),
               (pad, GRID - 1 - pad, 1, -1), (GRID - 1 - pad, GRID - 1 - pad, -1, -1))
    for (x0, y0, sx, sy) in corners:
        xa, xb = sorted([x0, x0 + sx * (arm - 1)])
        ya, yb = sorted([y0, y0 + sy * (thick - 1)])
        d.rectangle([xa, ya, xb, yb], fill=RED)          # 横臂
        xa, xb = sorted([x0, x0 + sx * (thick - 1)])
        ya, yb = sorted([y0, y0 + sy * (arm - 1)])
        d.rectangle([xa, ya, xb, yb], fill=RED)          # 竖臂

    # 中央实心方块（不挖孔，16px 下才不会糊成一团）
    d.rectangle([13, 13, 18, 18], fill=RED)

    # 四向准星，2 格宽
    d.rectangle([15, 8, 16, 10], fill=RED)
    d.rectangle([15, 21, 16, 23], fill=RED)
    d.rectangle([8, 15, 10, 16], fill=RED)
    d.rectangle([21, 15, 23, 16], fill=RED)
    return im


def main() -> int:
    small = build()
    big = small.resize((256, 256), Image.NEAREST)

    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    ico = assets / "icon.ico"
    big.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(assets / "icon.png")
    # 预览：放大 4 倍看像素结构
    big.resize((512, 512), Image.NEAREST).save(ROOT / "icon_preview.png")
    print("wrote", ico, ico.stat().st_size, "bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
