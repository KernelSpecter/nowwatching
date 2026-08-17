"""Generate NowWatching's PNG icons at 16/48/128.

A play triangle on Discord blurple. Drawn at 512 and downsampled with LANCZOS
so the 16px version keeps clean edges. Re-run any time to tweak the mark.

    python tools/make_icons.py

Requires Pillow. Nothing else in this project does, which is why this lives in
tools/ rather than being part of the build.
"""
from pathlib import Path

from PIL import Image, ImageDraw

S = 512
BLURPLE = (88, 101, 242, 255)
WHITE = (255, 255, 255, 255)

OUT = Path(__file__).resolve().parent.parent / "extension" / "icons"


def render(size: int) -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Flat rounded square. A gradient or a shadow band was tried here and read
    # as a rendering artifact at 16px rather than as depth.
    d.rounded_rectangle([6, 6, S - 6, S - 6], radius=112, fill=BLURPLE)

    # Play triangle. Nudged right of centre: the optical centre of a triangle
    # sits left of its bounding box, so a mathematically centred one looks off.
    cx, cy = S // 2 + 18, S // 2
    r = 132
    d.polygon(
        [(cx - r * 0.82, cy - r), (cx - r * 0.82, cy + r), (cx + r * 0.95, cy)],
        fill=WHITE,
    )

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for sz in (16, 48, 128):
        render(sz).save(OUT / f"icon{sz}.png")
    render(256).save(OUT / "preview256.png")
    print(f"icons written to {OUT}")


if __name__ == "__main__":
    main()
