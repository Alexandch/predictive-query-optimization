"""Generate the deterministic Windows application icon."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


def generate_icon(output_path: str | Path) -> Path:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    size = 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    for y in range(18, 238):
        ratio = (y - 18) / 220
        color = (
            int(18 + 12 * ratio),
            int(52 + 35 * ratio),
            int(86 + 72 * ratio),
            255,
        )
        draw.line((18, y, 238, y), fill=color, width=1)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((18, 18, 238, 238), radius=48, fill=255)
    image.putalpha(mask)
    draw = ImageDraw.Draw(image)

    blue = (96, 165, 250, 255)
    white = (239, 246, 255, 255)
    green = (52, 211, 153, 255)
    draw.ellipse((54, 58, 178, 104), fill=blue, outline=white, width=6)
    draw.rectangle((54, 80, 178, 163), fill=blue)
    draw.line((54, 82, 54, 160), fill=white, width=6)
    draw.line((178, 82, 178, 160), fill=white, width=6)
    draw.arc((54, 111, 178, 157), 0, 180, fill=white, width=6)
    draw.arc((54, 139, 178, 185), 0, 180, fill=white, width=6)
    draw.arc((54, 137, 178, 183), 0, 180, fill=blue, width=5)
    points = ((73, 190), (112, 158), (142, 176), (198, 112))
    draw.line(points, fill=(7, 20, 36, 190), width=17, joint="curve")
    draw.line(points, fill=green, width=10, joint="curve")
    draw.polygon(((198, 112), (180, 116), (194, 132)), fill=green)

    image.save(
        destination,
        format="ICO",
        sizes=((16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)),
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(generate_icon(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
