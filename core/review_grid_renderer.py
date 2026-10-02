"""Larger 3x3 previews, independent of the all-images contact sheet."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


def file_stamp(path: str) -> tuple[int, int] | None:
    try:
        stat = Path(path).stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError:
        return None


def preview_image(path: str) -> Image.Image:
    stamp = file_stamp(path)
    if stamp is None or stamp[0] > 40 * 1024 * 1024:
        raise OSError('preview unavailable')
    # One bounded read avoids thousands of small reads across the Windows bind mount.
    with Image.open(BytesIO(Path(path).read_bytes())) as source:
        source.draft('RGB', (640, 640))
        thumb = ImageOps.exif_transpose(source)
        thumb.thumbnail((320, 320), Image.Resampling.LANCZOS)
        result = thumb.convert('RGBA')
        thumb.close()
        return result


def _font(size: int):
    for filename in ('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
                     '/AstrBot/data/plugins/astrbot_plugin_essential/simhei.ttf',
                     'C:/Windows/Fonts/msyh.ttc', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        if Path(filename).is_file():
            return ImageFont.truetype(filename, size)
    return ImageFont.load_default(size=size)


def _lines(text: str, draw, font, width: int = 320) -> list[str]:
    lines = ['']
    for char in text.replace('\n', ' ').replace('\r', ' '):
        if draw.textlength(lines[-1] + char, font=font) > width:
            if len(lines) == 2:
                lines[-1] = lines[-1][:-1] + '…'
                break
            lines.append('')
        lines[-1] += char
    return lines


def render_review_grid(rows: list[dict], path: Path, *, heading: str) -> dict:
    if not 1 <= len(rows) <= 9:
        raise ValueError('nine or fewer previews required')
    canvas = Image.new('RGB', (1020, 1282), '#e7effb')
    draw = ImageDraw.Draw(canvas)
    font, title_font = _font(19), _font(25)
    draw.text((12, 12), heading, font=title_font, fill='#142238')
    draw.text((12, 48), '引用本条：看第3张 / 通过3 角色名 / 拒绝3 / 跳过3 / 下一页', font=font, fill='#40516b')
    readable, stamps = [], {}
    for index, row in enumerate(rows):
        x, y = (index % 3) * 340 + 10, 82 + (index // 3) * 394
        draw.rectangle((x, y, x + 320, y + 320), fill='white')
        before = file_stamp(row['file_path'])
        try:
            thumb = preview_image(row['file_path'])
            canvas.paste(thumb, (x + (320 - thumb.width) // 2, y + (320 - thumb.height) // 2), thumb)
            thumb.close()
            if before == file_stamp(row['file_path']):
                readable.append(row['image_id'])
                stamps[row['image_id']] = before
        except (OSError, ValueError, Image.DecompressionBombError):
            draw.text((x + 68, y + 135), '图片不可读取', font=title_font, fill='#586579')
        label = f'{index + 1}  #{row["image_id"]}  {row["status_label"]}'
        draw.text((x, y + 324), label, font=font, fill='#142238')
        for offset, line in enumerate(_lines('、'.join(row['candidate_names']) or '无待审候选标签', draw, font)):
            draw.text((x, y + 348 + offset * 21), line, font=font, fill='#40516b')
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, format='PNG', compress_level=3)
    canvas.close()
    return {'readable': readable, 'stamps': stamps}
