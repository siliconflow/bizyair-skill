"""cover.py — placeholder cover generation for model publishing (pure Python, no third-party deps).

When the user has no cover ready, `model-publish` generates a 1024x1024 placeholder:
  - Background: two-color gradient + diagonal texture, seeded by a hash of "model name + type"
  - Text: model type (large), model name (ASCII-displayable), base model (small)
  - Drawn with a 5x7 bitmap font, no reliance on PIL / system fonts / CJK fonts

The generated PNG goes through exactly the same upload flow as a local cover;
the server then converts it to WebP.
"""
from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

SIZE = 1024

# 5x7 bitmap font: 7 rows per character, 5 bits per row ('1' = lit)
FONT: dict[str, tuple[str, ...]] = {
    'A': ('01110', '10001', '10001', '11111', '10001', '10001', '10001'),
    'B': ('11110', '10001', '10001', '11110', '10001', '10001', '11110'),
    'C': ('01110', '10001', '10000', '10000', '10000', '10001', '01110'),
    'D': ('11110', '10001', '10001', '10001', '10001', '10001', '11110'),
    'E': ('11111', '10000', '10000', '11110', '10000', '10000', '11111'),
    'F': ('11111', '10000', '10000', '11110', '10000', '10000', '10000'),
    'G': ('01110', '10001', '10000', '10111', '10001', '10001', '01111'),
    'H': ('10001', '10001', '10001', '11111', '10001', '10001', '10001'),
    'I': ('01110', '00100', '00100', '00100', '00100', '00100', '01110'),
    'J': ('00111', '00010', '00010', '00010', '00010', '10010', '01100'),
    'K': ('10001', '10010', '10100', '11000', '10100', '10010', '10001'),
    'L': ('10000', '10000', '10000', '10000', '10000', '10000', '11111'),
    'M': ('10001', '11011', '10101', '10101', '10001', '10001', '10001'),
    'N': ('10001', '11001', '10101', '10011', '10001', '10001', '10001'),
    'O': ('01110', '10001', '10001', '10001', '10001', '10001', '01110'),
    'P': ('11110', '10001', '10001', '11110', '10000', '10000', '10000'),
    'Q': ('01110', '10001', '10001', '10001', '10101', '10010', '01101'),
    'R': ('11110', '10001', '10001', '11110', '10100', '10010', '10001'),
    'S': ('01111', '10000', '10000', '01110', '00001', '00001', '11110'),
    'T': ('11111', '00100', '00100', '00100', '00100', '00100', '00100'),
    'U': ('10001', '10001', '10001', '10001', '10001', '10001', '01110'),
    'V': ('10001', '10001', '10001', '10001', '10001', '01010', '00100'),
    'W': ('10001', '10001', '10001', '10101', '10101', '10101', '01010'),
    'X': ('10001', '10001', '01010', '00100', '01010', '10001', '10001'),
    'Y': ('10001', '10001', '01010', '00100', '00100', '00100', '00100'),
    'Z': ('11111', '00001', '00010', '00100', '01000', '10000', '11111'),
    '0': ('01110', '10001', '10011', '10101', '11001', '10001', '01110'),
    '1': ('00100', '01100', '00100', '00100', '00100', '00100', '01110'),
    '2': ('01110', '10001', '00001', '00010', '00100', '01000', '11111'),
    '3': ('11110', '00001', '00001', '01110', '00001', '00001', '11110'),
    '4': ('00010', '00110', '01010', '10010', '11111', '00010', '00010'),
    '5': ('11111', '10000', '11110', '00001', '00001', '10001', '01110'),
    '6': ('00110', '01000', '10000', '11110', '10001', '10001', '01110'),
    '7': ('11111', '00001', '00010', '00100', '01000', '01000', '01000'),
    '8': ('01110', '10001', '10001', '01110', '10001', '10001', '01110'),
    '9': ('01110', '10001', '10001', '01111', '00001', '00010', '01100'),
    '-': ('00000', '00000', '00000', '11111', '00000', '00000', '00000'),
    '_': ('00000', '00000', '00000', '00000', '00000', '00000', '11111'),
    '.': ('00000', '00000', '00000', '00000', '00000', '01100', '01100'),
    '/': ('00001', '00010', '00010', '00100', '01000', '01000', '10000'),
    ':': ('00000', '01100', '01100', '00000', '01100', '01100', '00000'),
    ' ': ('00000', '00000', '00000', '00000', '00000', '00000', '00000'),
}

GLYPH_W = 5
GLYPH_H = 7


def _palette(seed: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    digest = hashlib.sha256(seed.encode('utf-8')).digest()
    hue_a = digest[0] / 255.0
    hue_b = (hue_a + 0.25 + digest[1] / 510.0) % 1.0

    def hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
        i = int(h * 6)
        f = h * 6 - i
        p = v * (1 - s)
        q = v * (1 - f * s)
        t = v * (1 - (1 - f) * s)
        r, g, b = [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][i % 6]
        return int(r * 255), int(g * 255), int(b * 255)

    return hsv_to_rgb(hue_a, 0.62, 0.30), hsv_to_rgb(hue_b, 0.55, 0.62)


def _blend(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


def _draw_text(pixels: list[list[tuple[int, int, int]]], text: str, *, scale: int, center_y: int, color: tuple[int, int, int]) -> int:
    """Draw one line of 5x7 bitmap text into the pixel buffer, returning the drawn width; unsupported characters are treated as spaces."""
    glyphs = []
    for char in text.upper():
        glyph = FONT.get(char)
        if glyph is None:
            glyph = FONT[' ']
        glyphs.append(glyph)
    if not glyphs:
        return 0
    advance = (GLYPH_W + 1) * scale
    total_w = advance * len(glyphs) - scale
    start_x = (SIZE - total_w) // 2
    top = center_y - (GLYPH_H * scale) // 2
    for index, glyph in enumerate(glyphs):
        origin_x = start_x + index * advance
        for row_index, row in enumerate(glyph):
            for col_index, bit in enumerate(row):
                if bit != '1':
                    continue
                x0 = origin_x + col_index * scale
                y0 = top + row_index * scale
                for dy in range(scale):
                    y = y0 + dy
                    if not 0 <= y < SIZE:
                        continue
                    line = pixels[y]
                    for dx in range(scale):
                        x = x0 + dx
                        if 0 <= x < SIZE:
                            line[x] = color
    return total_w


def _fit_scale(text: str, max_width: int, preferred: int) -> int:
    if not text:
        return preferred
    width_at_one = (GLYPH_W + 1) * len(text) - 1
    if width_at_one <= 0:
        return preferred
    return max(2, min(preferred, max_width // width_at_one))


def _write_png(path: Path, pixels: list[list[tuple[int, int, int]]]) -> None:
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0
        for r, g, b in row:
            raw.extend((r & 0xFF, g & 0xFF, b & 0xFF))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack('>IIBBBBB', SIZE, SIZE, 8, 2, 0, 0, 0)
    payload = (
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', header)
        + chunk(b'IDAT', zlib.compress(bytes(raw), 9))
        + chunk(b'IEND', b'')
    )
    path.write_bytes(payload)


def render_cover(path: str | Path, *, name: str, model_type: str, base_model: str = '') -> Path:
    """Generate a 1024x1024 placeholder cover PNG and return its path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    dark, light = _palette(f'{name}|{model_type}')
    accent = _blend(light, (255, 255, 255), 0.55)
    pixels: list[list[tuple[int, int, int]]] = []
    for y in range(SIZE):
        dy = y / (SIZE - 1)
        row: list[tuple[int, int, int]] = []
        for x in range(SIZE):
            dx = x / (SIZE - 1)
            t = min(1.0, (dx * 0.55 + dy * 0.75))
            color = _blend(dark, light, t)
            # Diagonal fine texture so the placeholder does not look too flat
            if ((x + y) // 26) % 9 == 0:
                color = _blend(color, light, 0.18)
            # Slightly darken the four corners
            edge = min(dx, dy, 1 - dx, 1 - dy)
            if edge < 0.12:
                color = _blend(color, dark, (0.12 - edge) * 2.2)
            row.append(color)
        pixels.append(row)

    # A highlight horizontal line in the middle acts as the visual anchor
    for y in range(430, 438):
        for x in range(96, SIZE - 96):
            pixels[y][x] = _blend(pixels[y][x], accent, 0.5)

    type_scale = _fit_scale(model_type, SIZE - 220, 30)
    _draw_text(pixels, model_type, scale=type_scale, center_y=445, color=accent)

    ascii_name = ''.join(char if ord(char) < 128 else ' ' for char in name).strip()
    if ascii_name:
        name_scale = _fit_scale(ascii_name, SIZE - 260, 14)
        _draw_text(pixels, ascii_name[:40], scale=name_scale, center_y=665, color=_blend(accent, light, 0.2))

    if base_model:
        ascii_base = ''.join(char if ord(char) < 128 else ' ' for char in base_model).strip()
        if ascii_base:
            base_scale = _fit_scale(ascii_base, SIZE - 320, 8)
            _draw_text(pixels, ascii_base[:48], scale=base_scale, center_y=745, color=_blend(light, (255, 255, 255), 0.35))

    _write_png(target, pixels)
    return target
