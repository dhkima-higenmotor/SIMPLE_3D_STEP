"""Generate the SIMPLE 3D STEP application icon (PNG sizes + multi-size ICO).

Run from the repository root:
    uv run python tools/make_icon.py

The icon is a cobalt rounded tile with an isometric solid cube:
  * the front face carries an engraved gear  -> mechanical STEP assembly internals
  * the clean, seamless cube                 -> one watertight solid body
  * the emerald check badge                  -> "simplification complete"

Small icon sizes (16/24/32 px) use a compact variant without the gear engraving
so the cube and checkmark stay legible in taskbars and file lists.

Outputs:
    docs/images/icon.png                 512 px master (used by README)
    docs/images/icon_sizes_preview.png   all sizes on light/dark backgrounds
    src/simple_3d_step/gui/assets/app.ico  window/taskbar icon (16-256 px)

Requires Pillow (already installed transitively via scikit-image -> imageio).
"""

from __future__ import annotations

import io
import math
import struct
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
PNG_DIR = ROOT / "docs" / "images"
ICO_PATH = ROOT / "src" / "simple_3d_step" / "gui" / "assets" / "app.ico"

SIZE = 1024
MARGIN = 56
CORNER_RADIUS = 230

COBALT_TOP = (30, 64, 175)      # #1E40AF
COBALT_BOTTOM = (14, 165, 233)  # #0EA5E9
FACE_TOP = (255, 255, 255)
FACE_LEFT = (233, 241, 253)
FACE_RIGHT = (197, 216, 242)
GEAR_INK = (37, 99, 235, 62)    # engraved translucent cobalt
EMERALD = (5, 150, 105)         # #059669

DETAIL_SIZES = (48, 64, 128, 256)
COMPACT_SIZES = (16, 24, 32)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def rounded_tile_mask() -> Image.Image:
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (MARGIN, MARGIN, SIZE - 1 - MARGIN, SIZE - 1 - MARGIN),
        radius=CORNER_RADIUS,
        fill=255,
    )
    return mask


def gradient_tile() -> Image.Image:
    yy, xx = np.mgrid[0:SIZE, 0:SIZE].astype(np.float32)
    t = np.clip((xx + yy) / float(2 * SIZE - 2), 0.0, 1.0)[..., None]
    top = np.array(COBALT_TOP, dtype=np.float32)
    bottom = np.array(COBALT_BOTTOM, dtype=np.float32)
    rgb = top * (1.0 - t) + bottom * t

    # Soft top-left highlight for a subtle glassy depth
    cx = int(SIZE * 0.28)
    cy = int(SIZE * 0.20)
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / (SIZE * 0.62)
    glow = np.clip(1.0 - dist, 0.0, 1.0)[..., None] ** 2 * 0.22
    rgb = rgb * (1.0 - glow) + 255.0 * glow
    return Image.fromarray(rgb.astype(np.uint8), mode="RGB").convert("RGBA")


def draw_cube(
    draw: ImageDraw.ImageDraw, cx: float, cy: float, w: float, v: float, edge_alpha: int
) -> dict:
    """Draw an isometric solid cube and return its key vertices."""
    h = w * math.tan(math.radians(30.0))  # rhombus half-height of the top face

    top = (cx, cy - h)
    right = (cx + w, cy)
    center = (cx, cy + h)
    left = (cx - w, cy)
    right_low = (cx + w, cy + v)
    front_low = (cx, cy + h + v)
    left_low = (cx - w, cy + v)

    draw.polygon([top, right, center, left], fill=FACE_TOP)              # top face
    draw.polygon([left, center, front_low, left_low], fill=FACE_LEFT)    # left face
    draw.polygon([center, right, right_low, front_low], fill=FACE_RIGHT)  # right face

    # Hairline edges keep the faces legible at small sizes
    edge = (37, 99, 235, edge_alpha)
    draw.line([top, right, center, left, top], fill=edge, width=3, joint="curve")
    draw.line([left, left_low, front_low, right_low, right], fill=edge, width=3, joint="curve")
    draw.line([center, front_low], fill=edge, width=3)

    return {
        "top": top,
        "left": left,
        "center": center,
        "right": right,
        "left_low": left_low,
        "front_low": front_low,
        "right_low": right_low,
    }


def gear_polygon(
    teeth: int = 12, r_out: float = 1.0, r_in: float = 0.78
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    step = 2.0 * math.pi / teeth
    for i in range(teeth):
        base = i * step
        for radius, offset in (
            (r_out, 0.00),
            (r_out, 0.24),
            (r_in, 0.33),
            (r_in, 0.67),
            (r_out, 0.76),
        ):
            angle = base + step * offset
            points.append((radius * math.cos(angle), radius * math.sin(angle)))
    return points


def draw_engraved_gear(canvas: Image.Image, verts: dict) -> None:
    """Map a gear onto the isometric left face (affine plane projection)."""
    origin = verts["left"]
    u = (verts["center"][0] - origin[0], verts["center"][1] - origin[1])
    v = (verts["left_low"][0] - origin[0], verts["left_low"][1] - origin[1])

    def to_screen(uu: float, vv: float) -> tuple[float, float]:
        return (origin[0] + u[0] * uu + v[0] * vv, origin[1] + u[1] * uu + v[1] * vv)

    # Face-local gear footprint (uu: along the iso edge, vv: vertical)
    footprint = 0.44
    gear_pts = [
        to_screen(0.5 + x * footprint, 0.54 + y * footprint)
        for x, y in gear_polygon()
    ]
    hub_pts = [
        to_screen(0.5 + x * footprint * 0.34, 0.54 + y * footprint * 0.34)
        for x, y in gear_polygon(teeth=12, r_out=1.0, r_in=0.95)
    ]

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    layer_draw.polygon(gear_pts, fill=GEAR_INK)
    layer_draw.polygon(hub_pts, fill=(233, 241, 253, 255))  # punched hub = face color

    # Clip to the left face so the engraving never spills onto other faces
    face_mask = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(face_mask).polygon(
        [verts["left"], verts["center"], verts["front_low"], verts["left_low"]], fill=255
    )
    canvas.paste(Image.alpha_composite(canvas, layer), (0, 0), face_mask)


def draw_check_badge(canvas: Image.Image, cx: float, cy: float, radius: float) -> None:
    draw = ImageDraw.Draw(canvas)
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=EMERALD,
        outline=(255, 255, 255, 255),
        width=max(2, int(radius * 0.16)),
    )
    check = [
        (cx - radius * 0.46, cy + radius * 0.02),
        (cx - radius * 0.10, cy + radius * 0.38),
        (cx + radius * 0.50, cy - radius * 0.34),
    ]
    draw.line(check, fill=(255, 255, 255, 255), width=max(2, int(radius * 0.24)), joint="curve")


# ----------------------------------------------------------------------------
# Composition
# ----------------------------------------------------------------------------

def build_master(compact: bool = False) -> Image.Image:
    mask = rounded_tile_mask()

    # Drop shadow (kept fully inside the canvas so no hard cut appears)
    shadow_offset = 14 if compact else 18
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (MARGIN + 4, MARGIN + shadow_offset, SIZE - 1 - MARGIN + 4, SIZE - 1 - MARGIN + shadow_offset),
        radius=CORNER_RADIUS,
        fill=(6, 20, 45, 112 if compact else 120),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(26 if compact else 30))

    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas = Image.alpha_composite(canvas, shadow)

    tile = gradient_tile()
    tile.putalpha(mask)
    canvas = Image.alpha_composite(canvas, tile)

    # Thin inner rim
    rim = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(rim).rounded_rectangle(
        (MARGIN + 3, MARGIN + 3, SIZE - 1 - MARGIN - 3, SIZE - 1 - MARGIN - 3),
        radius=CORNER_RADIUS - 4,
        outline=(255, 255, 255, 46),
        width=3,
    )
    canvas = Image.alpha_composite(canvas, rim)

    if compact:
        cx, cy = SIZE / 2, SIZE * 0.47
        w, v = SIZE * 0.268, SIZE * 0.238
        edge_alpha = 62
    else:
        cx, cy = SIZE / 2, SIZE * 0.455
        w, v = SIZE * 0.235, SIZE * 0.215
        edge_alpha = 34

    cube = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    verts = draw_cube(ImageDraw.Draw(cube), cx, cy, w, v, edge_alpha)
    if not compact:
        draw_engraved_gear(cube, verts)
    canvas = Image.alpha_composite(canvas, cube)

    # Completion badge at the cube's lower-right corner
    badge_r = SIZE * (0.148 if compact else 0.125)
    draw_check_badge(canvas, cx + w * 0.90, cy + v * 1.00, badge_r)

    return canvas


def write_ico(path: Path, images: dict[int, Image.Image]) -> None:
    """Write a multi-resolution ICO made of PNG-compressed frames (Vista+)."""
    frames = sorted(images.items())
    blobs: list[bytes] = []
    for _size, img in frames:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        blobs.append(buf.getvalue())

    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries = bytearray()
    for (size, _img), blob in zip(frames, blobs):
        dim = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset
        )
        offset += len(blob)

    path.write_bytes(header + bytes(entries) + b"".join(blobs))


def build_preview_sheet(images: dict[int, Image.Image]) -> Image.Image:
    """Compose all icon sizes on light and dark backgrounds for visual QA."""
    cell = 256
    sheet = Image.new("RGBA", (cell * len(images), cell * 2), (0, 0, 0, 0))
    for i, size in enumerate(sorted(images)):
        img = images[size]
        for row, bg in ((0, (245, 247, 250, 255)), (1, (15, 23, 42, 255))):
            tile = Image.new("RGBA", (cell, cell), bg)
            tile.alpha_composite(img, ((cell - size) // 2, (cell - size) // 2))
            sheet.alpha_composite(tile, (i * cell, row * cell))
    sheet.save(PNG_DIR / "icon_sizes_preview.png")
    return sheet


def main() -> None:
    PNG_DIR.mkdir(parents=True, exist_ok=True)
    ICO_PATH.parent.mkdir(parents=True, exist_ok=True)

    detail = build_master()
    compact = build_master(compact=True)

    images: dict[int, Image.Image] = {}
    for size in DETAIL_SIZES:
        images[size] = detail.resize((size, size), Image.LANCZOS)
    for size in COMPACT_SIZES:
        images[size] = compact.resize((size, size), Image.LANCZOS)

    detail.resize((512, 512), Image.LANCZOS).save(PNG_DIR / "icon.png")
    build_preview_sheet(images)
    write_ico(ICO_PATH, images)
    print("wrote", PNG_DIR / "icon.png")
    print("wrote", PNG_DIR / "icon_sizes_preview.png")
    print("wrote", ICO_PATH, sorted(images))


if __name__ == "__main__":
    main()
