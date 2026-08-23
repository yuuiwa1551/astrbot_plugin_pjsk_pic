from __future__ import annotations

from typing import Any


def expose_group_subcommands_at_root(group_command: Any) -> tuple[str, ...]:
    """Expose each direct subcommand's primary name without its group name.

    AstrBot has already removed the configured wake prefix when command filters
    run.  This helper therefore only makes ``.子命令`` equivalent to
    ``.pp 子命令``; it does not allow bare, non-waking chat text to trigger a
    command.  Child aliases stay group-scoped so generic aliases such as
    ``help`` cannot collide with existing top-level commands.
    """

    parent_group = getattr(group_command, "parent_group", None)
    if parent_group is None:
        raise ValueError("command group is missing parent_group")

    exposed: list[str] = []
    for command_filter in getattr(parent_group, "sub_command_filters", ()):
        command_name = str(getattr(command_filter, "command_name", "") or "").strip()
        get_complete_command_names = getattr(
            command_filter,
            "get_complete_command_names",
            None,
        )
        if not command_name or not callable(get_complete_command_names):
            continue

        complete_names = list(get_complete_command_names())
        if command_name not in complete_names:
            complete_names.append(command_name)
            # AstrBot caches this list on CommandFilter.  Updating the cache
            # keeps the original group paths and adds only the primary root
            # name, instead of also promoting potentially conflicting aliases.
            command_filter._cmpl_cmd_names = complete_names
        exposed.append(command_name)

    return tuple(exposed)
