"""Exercise actual plugin parser without starting workers or loading live data."""
import ast
import re
import unicodedata
import unittest
from pathlib import Path

tree = ast.parse((Path(__file__).resolve().parents[1] / 'main.py').read_text(encoding='utf-8'))
plugin = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PJSKPicPlugin')
body = [n for n in plugin.body if (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'DIRECT_IMAGE_ID_PATTERN' for t in n.targets)) or (isinstance(n, ast.FunctionDef) and n.name == '_parse_direct_image_id')]
shell = ast.ClassDef(name='Parser', bases=[], keywords=[], body=body, decorator_list=[])
namespace = {'re': re, 'unicodedata': unicodedata}
exec(compile(ast.fix_missing_locations(ast.Module(body=[shell], type_ignores=[])), 'parser', 'exec'), namespace)


class ImageIdSyntaxTests(unittest.TestCase):
    def test_new_and_existing_syntax(self):
        parser = namespace['Parser']()
        for text in ('看图1234', '看图 #1234', '看看id1234', '看图片编号１２３４'):
            self.assertEqual(1234, parser._parse_direct_image_id(text))
        for text in ('1234', '看1234', '今天看图1234不错', '看图1234和5678'):
            self.assertIsNone(parser._parse_direct_image_id(text))
