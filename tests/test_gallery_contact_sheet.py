import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path

from PIL import Image

pkg = types.ModuleType('sheet_tests_core')
pkg.__path__ = [str(Path(__file__).resolve().parents[1] / 'core')]
sys.modules[pkg.__name__] = pkg
sheet = importlib.import_module(pkg.__name__ + '.gallery_contact_sheet')
DB = importlib.import_module(pkg.__name__ + '.db').ImageIndexDB


class ContactSheetTests(unittest.TestCase):
    def test_commands_do_not_capture_regular_queries(self):
        for text in ['看初音 all', '看所有初音', '看 初音 ALL', '看初音 ａｌｌ']:
            self.assertEqual('初音', sheet.parse_contact_query(text))
        for text in ['看初音', '看图1234', '看所有', '看初音 all 再说']:
            self.assertIsNone(sheet.parse_contact_query(text))

    def test_full_length_with_missing_and_portrait(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'portrait.png'
            Image.new('RGB', (20, 100), 'red').save(source)
            rows = [{'id': n, 'file_path': str(source)} for n in range(1, 24)]
            rows[-1]['file_path'] = str(root / 'missing.png')
            result = sheet.render_contact_sheet(rows, root / 'out.png')
            self.assertEqual(23, result['count'])
            self.assertEqual(1, result['missing'])
            with Image.open(root / 'out.png') as image:
                image.load()
                self.assertEqual((1400, 524), image.size)
                self.assertEqual((255, 0, 0), image.getpixel((70, 90)))
                self.assertNotEqual((255, 0, 0), image.getpixel((10, 90)))
                self.assertEqual((213, 217, 224), image.getpixel((300, 380)))

    def test_over_jpeg_height_stays_one_png(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'long.png'
            result = sheet.render_contact_sheet([{'id': n, 'file_path': 'missing'} for n in range(4100)], path)
            self.assertEqual(65644, result['height'])
            with Image.open(path) as image:
                self.assertEqual((1400, 65644), image.size)
            self.assertEqual([path], list(Path(directory).iterdir()))

    def test_database_uses_sendable_status_and_no_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DB(Path(directory) / 'db.sqlite')
            tag = db.get_or_create_tag('初音未来', tag_type='character')
            with db._connect() as conn:
                for n, status in enumerate(['approved', 'manual_approved', 'pending', 'rejected'], 1):
                    conn.execute("INSERT INTO images(id,file_path,file_name,sha256,is_active,created_at,updated_at) VALUES(?,?,?,?,1,'now','now')", (n, str(n), str(n), str(n)))
                    conn.execute("INSERT INTO image_tags(image_id,tag_id,review_status,source_type,created_at,updated_at) VALUES(?,?,?,'test','now','now')", (n, tag, status))
                conn.execute('UPDATE images SET is_active=0 WHERE id=2')
            self.assertEqual([1], [r['id'] for r in db.list_sendable_images_for_tag(tag)])
