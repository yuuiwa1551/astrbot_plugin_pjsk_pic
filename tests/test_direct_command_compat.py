from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "core" / "command_compat.py"
SPEC = importlib.util.spec_from_file_location("pjsk_command_compat", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"无法加载测试模块：{MODULE_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
expose_group_subcommands_at_root = MODULE.expose_group_subcommands_at_root


class FakeCommandFilter:
    def __init__(self, command_name: str, aliases: set[str] | None = None) -> None:
        self.command_name = command_name
        self.alias = aliases or set()
        self._cmpl_cmd_names: list[str] | None = None

    def get_complete_command_names(self) -> list[str]:
        if self._cmpl_cmd_names is None:
            self._cmpl_cmd_names = [
                f"{parent} {name}"
                for name in [self.command_name, *sorted(self.alias)]
                for parent in ("pjsk图库", "pp")
            ]
        return self._cmpl_cmd_names


class FakeParentGroup:
    def __init__(self, subcommands: list[FakeCommandFilter]) -> None:
        self.sub_command_filters = subcommands


class FakeGroupCommand:
    def __init__(self, subcommands: list[FakeCommandFilter]) -> None:
        self.parent_group = FakeParentGroup(subcommands)


class DirectCommandCompatibilityTests(unittest.TestCase):
    def test_primary_names_are_exposed_without_promoting_aliases(self):
        help_command = FakeCommandFilter("帮助", {"help", "菜单"})
        merge_command = FakeCommandFilter("tag合并")
        group = FakeGroupCommand([help_command, merge_command])

        exposed = expose_group_subcommands_at_root(group)

        self.assertEqual(exposed, ("帮助", "tag合并"))
        self.assertIn("帮助", help_command.get_complete_command_names())
        self.assertNotIn("help", help_command.get_complete_command_names())
        self.assertNotIn("菜单", help_command.get_complete_command_names())
        self.assertIn("pp help", help_command.get_complete_command_names())
        self.assertIn("pjsk图库 菜单", help_command.get_complete_command_names())
        self.assertIn("tag合并", merge_command.get_complete_command_names())
        self.assertIn("pp tag合并", merge_command.get_complete_command_names())

    def test_registration_is_idempotent(self):
        command = FakeCommandFilter("统计")
        group = FakeGroupCommand([command])

        expose_group_subcommands_at_root(group)
        expose_group_subcommands_at_root(group)

        self.assertEqual(command.get_complete_command_names().count("统计"), 1)


if __name__ == "__main__":
    unittest.main()
