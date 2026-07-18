from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SLASH_COMMAND_PATTERN = re.compile(
    r"(?<!:)/(?:pp|pjsk图库|投稿|tg|alias|unalias)"
    r"(?=$|[\s`'\"，。,.:：;；!?！？<>()（）\[\]【】])",
    re.IGNORECASE,
)
USER_MESSAGE_FILES = (
    REPO_ROOT / "main.py",
    REPO_ROOT / "core" / "submission_notify_service.py",
    REPO_ROOT / "core" / "submission_service.py",
)
DOCUMENT_FILES = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "_conf_schema.json",
)


def iter_string_constants(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


class CommandHintPrefixTests(unittest.TestCase):
    def test_user_visible_python_messages_do_not_use_slash_commands(self):
        failures: list[str] = []
        for path in USER_MESSAGE_FILES:
            for line_number, value in iter_string_constants(path):
                if SLASH_COMMAND_PATTERN.search(value):
                    failures.append(f"{path.name}:{line_number}: {value!r}")
        self.assertEqual(failures, [])

    def test_docs_and_config_do_not_use_slash_command_examples(self):
        failures: list[str] = []
        for path in DOCUMENT_FILES:
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8-sig").splitlines(),
                start=1,
            ):
                if SLASH_COMMAND_PATTERN.search(line):
                    failures.append(f"{path.name}:{line_number}: {line}")
        self.assertEqual(failures, [])

    def test_pixiv_review_card_contains_dot_commands(self):
        constants = [
            value
            for _, value in iter_string_constants(REPO_ROOT / "main.py")
        ]
        self.assertIn("通过并归类：.pp 审图通过 <最终tag>", constants)
        self.assertIn("整图不要：.pp 审图拒绝 [原因]", constants)
        self.assertIn("换一张：.pp 审图跳过", constants)


if __name__ == "__main__":
    unittest.main()
