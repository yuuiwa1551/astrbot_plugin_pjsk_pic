from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType('pjsk_identity_b_tests')
pkg.__path__ = [str(ROOT / 'core')]
sys.modules[pkg.__name__] = pkg
profiles = importlib.import_module(pkg.__name__ + '.chat_image_identity_profiles')
evaluation = importlib.import_module(pkg.__name__ + '.identity_evaluation')
catalog = importlib.import_module(pkg.__name__ + '.chat_image_collection_service')


class ProfileTests(unittest.TestCase):
    def test_all_26_catalog_names_match_profiles(self):
        keys = set()
        for (name, english), japanese in zip(catalog.CHARACTERS, catalog.JAPANESE_NAMES):
            for field, value in [('name', name), ('name_en', english), ('name_ja', japanese)]:
                profile = profiles.resolve_profile({field: value})
                self.assertIsNotNone(profile, value)
                keys.add(profile['key'])
        self.assertEqual(26, len(keys))

    def test_off_does_not_read_profiles(self):
        with patch.object(profiles, 'load_profiles', side_effect=AssertionError('must not load')):
            self.assertEqual(('', 'none'), profiles.build_profile_context([], mode='off'))

    def test_only_allowed_character_ids_are_added(self):
        context, version = profiles.build_profile_context([
            {'tag_id': 7, 'name': '初音未来', 'tag_type': 'character'},
            {'tag_id': 9, 'name': '其他作品', 'tag_type': 'character'},
            {'tag_id': 20, 'name': '初音未来', 'tag_type': 'theme'},
        ], mode='text')
        self.assertIn('"tag_id":7', context)
        self.assertNotIn('"tag_id":9', context)
        self.assertNotIn('"tag_id":20', context)
        self.assertTrue(version.startswith('pjsk-text-'))
        self.assertEqual(profiles.build_profile_context([], mode='text'), ('', 'none'))


def fixture_manifest():
    return {'split_reviewed': True, 'roster': [{'key': 'miku'}, {'key': 'kanade'}], 'samples': [
        {'sample_id': 'a', 'group_id': 'g1', 'split': 'holdout', 'original_sha256': 'sha-a',
         'label': {'status': 'human_verified', 'complete': True, 'reviewer': 'test-human', 'is_target': True, 'character_keys': ['miku']}},
        {'sample_id': 'b', 'group_id': 'g2', 'split': 'holdout', 'original_sha256': 'sha-b',
         'label': {'status': 'human_verified', 'complete': True, 'reviewer': 'test-human', 'is_target': False, 'character_keys': []}},
    ]}


class EvaluationTests(unittest.TestCase):
    def test_unverified_labels_never_become_accuracy(self):
        manifest = fixture_manifest()
        for row in manifest['samples']:
            row['label']['status'] = 'pending'
        with self.assertRaisesRegex(ValueError, '没有人工核实'):
            evaluation.evaluate_identity(manifest, [])

    def test_split_review_and_leakage_are_required(self):
        manifest = fixture_manifest()
        manifest['split_reviewed'] = False
        with self.assertRaises(ValueError):
            evaluation.evaluate_identity(manifest, [])
        manifest['split_reviewed'] = True
        manifest['samples'][1].update(group_id='g1', split='development')
        with self.assertRaisesRegex(ValueError, '跨越'):
            evaluation.evaluate_identity(manifest, [])

    def test_missing_predictions_and_unknown_labels_are_rejected(self):
        manifest = fixture_manifest()
        with self.assertRaisesRegex(ValueError, '预测不完整'):
            evaluation.evaluate_identity(manifest, [])
        manifest['samples'][0]['label']['character_keys'] = ['unknown']
        with self.assertRaisesRegex(ValueError, '尚未完整核实'):
            evaluation.evaluate_identity(manifest, [])

    def test_role_metrics_and_processing_failures_are_separate(self):
        result = evaluation.evaluate_identity(fixture_manifest(), [
            {'sample_id': 'a', 'status': 'ok', 'character_keys': ['miku', 'kanade']},
            {'sample_id': 'b', 'status': 'error', 'character_keys': []},
        ])
        self.assertEqual(.5, result['precision'])
        self.assertEqual(1, result['recall'])
        self.assertEqual(0, result['exact_image_accuracy'])
        self.assertEqual(1, result['processing_errors'])
        self.assertEqual(['kanade'], result['missing_roles'])

    def test_groups_keep_same_origin_and_near_duplicates_together(self):
        rows = [
            {'sample_id': 'a', 'original_sha256': 'a', '_origins': ['post-x'], '_phash': '0000000000000000'},
            {'sample_id': 'b', 'original_sha256': 'b', '_origins': ['post-x'], '_phash': 'ffffffffffffffff'},
            {'sample_id': 'c', 'original_sha256': 'c', '_origins': [], '_phash': 'fffffffffffffff0'},
        ]
        evaluation.assign_sample_groups(rows)
        self.assertEqual(1, len({row['group_id'] for row in rows}))
        self.assertEqual(1, len({row['split'] for row in rows}))
        self.assertFalse(any('_origins' in row for row in rows))


class ExportTests(unittest.TestCase):
    def test_export_is_read_only_and_all_samples_start_pending(self):
        spec = importlib.util.spec_from_file_location('identity_review_export_test', ROOT / 'tools' / 'export_identity_review.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / 'index.db'
            with sqlite3.connect(db) as c:
                c.executescript('''
                    create table tags(id integer, name text, is_character integer);
                    create table image_tags(image_id integer, tag_id integer, review_status text);
                    create table images(id integer, file_path text, sha256 text, phash text, is_active integer);
                    create table sources(image_id integer, post_url text);
                    create table chat_image_candidates(id integer, status text);
                    insert into tags values(1,'初音未来',1);
                ''')
                for number in range(2):
                    path = root / f'{number}.png'
                    Image.new('RGB', (32, 32), (number * 100, 20, 30)).save(path)
                    c.execute('insert into images values(?,?,?,?,1)', (number, str(path), 'x', ''))
                    c.execute("insert into image_tags values(?,1,'approved')", (number,))
            before = hashlib.sha256(db.read_bytes()).hexdigest()
            output = root / 'review'
            summary = module.export_pack(db, output, limit=2)
            self.assertEqual(2, summary['exported'])
            self.assertEqual(0, summary['verified'])
            manifest = json.loads((output / 'manifest.json').read_text())
            self.assertTrue(all(s['label']['status'] == 'pending' for s in manifest['samples']))
            self.assertTrue(all(not s['label']['character_keys'] for s in manifest['samples']))
            self.assertFalse(manifest['split_reviewed'])
            self.assertEqual(before, hashlib.sha256(db.read_bytes()).hexdigest())
            with self.assertRaisesRegex(ValueError, '输出目录必须为空'):
                module.export_pack(db, output, limit=2)


if __name__ == '__main__':
    unittest.main()
