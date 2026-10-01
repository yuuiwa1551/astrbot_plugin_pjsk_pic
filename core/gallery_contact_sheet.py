"""Full-length contact sheets with bounded canvas memory (one row at a time)."""
from __future__ import annotations

import re
import struct
import unicodedata
import zlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


def parse_contact_query(text: str) -> str | None:
    text = unicodedata.normalize('NFKC', str(text or '')).strip()
    match = re.fullmatch(r'看所有\s*(.+?)\s*', text)
    if not match:
        match = re.fullmatch(r'看\s*(.+?)\s+all', text, re.IGNORECASE)
    return match.group(1).strip() if match and match.group(1).strip() else None


def _font(size: int):
    for filename in ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                     'C:/Windows/Fonts/arial.ttf'):
        if Path(filename).is_file():
            return ImageFont.truetype(filename, size)
    return ImageFont.load_default(size=size)


def render_contact_sheet(rows, destination: Path) -> dict:
    """Never truncate or paginate. Missing files retain their position and ID."""
    if not rows:
        raise ValueError('没有可展示的图片')
    columns, cell_w, cell_h, header_h = 10, 140, 160, 44
    width = columns * cell_w
    height = header_h + ((len(rows) + columns - 1) // columns) * cell_h
    background = '#e7effb'
    font = _font(18)
    missing = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('wb') as output:
        def chunk(kind, body):
            output.write(struct.pack('>I', len(body)) + kind + body
                         + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff))

        output.write(b'\x89PNG\r\n\x1a\n')
        chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
        compressor = zlib.compressobj(6)

        def stripe(canvas):
            raw = canvas.tobytes()
            stride = width * 3
            for start in range(0, len(raw), stride):
                compressed = compressor.compress(b'\x00' + raw[start:start + stride])
                if compressed:
                    chunk(b'IDAT', compressed)
            canvas.close()

        header = Image.new('RGB', (width, header_h), background)
        ImageDraw.Draw(header).text((10, 10), f'{len(rows)} IMAGES  |  GALLERY ID', font=font, fill='#172337')
        stripe(header)
        for start in range(0, len(rows), columns):
            canvas = Image.new('RGB', (width, cell_h), background)
            draw = ImageDraw.Draw(canvas)
            for col, row in enumerate(rows[start:start + columns]):
                left = col * cell_w
                try:
                    with Image.open(row['file_path']) as source:
                        source.draft('RGB', (264, 248))
                        thumb = ImageOps.exif_transpose(source)
                        thumb.thumbnail((132, 124), Image.Resampling.LANCZOS)
                        rgba = thumb.convert('RGBA')
                        canvas.paste(rgba, (left + (cell_w - rgba.width) // 2,
                                           4 + (124 - rgba.height) // 2), rgba)
                        rgba.close()
                        thumb.close()
                except (OSError, ValueError, Image.DecompressionBombError):
                    missing += 1
                    draw.rectangle((left + 4, 4, left + 135, 127), fill='#d5d9e0')
                    draw.text((left + 16, 50), 'MISSING', font=font, fill='#556070')
                label = str(row['id'])
                draw.text((left + cell_w / 2, 134), label, font=font, fill='#101820', anchor='mt')
            stripe(canvas)
        chunk(b'IDAT', compressor.flush())
        chunk(b'IEND', b'')
    return {'count': len(rows), 'missing': missing, 'width': width, 'height': height}
