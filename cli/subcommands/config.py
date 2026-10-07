"""Parser for the ``linteum config`` subcommand."""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_shared_flags, group_dispatch


def build_config_parser(subparsers, *, cmd_config: Callable) -> None:
    """Attach the ``config`` subcommand (including path / show / get / set) to ``subparsers``."""
    parser = subparsers.add_parser(
        "config", help="View / modify the config",
        description="Read and write config.yaml; set currently supports llm.<field>.")
    sub = parser.add_subparsers(dest="config_action")

    p_path = sub.add_parser("path", help="Print the config file path")
    add_shared_flags(p_path, inherited=True)
    p_path.set_defaults(func=cmd_config)

    p_show = sub.add_parser("show", help="Print the current config (YAML)")
    add_shared_flags(p_show, inherited=True)
    p_show.set_defaults(func=cmd_config)

    p_get = sub.add_parser("get", help="Read a config key (dotted path, e.g. llm.model)")
    add_shared_flags(p_get, inherited=True)
    p_get.add_argument("key", help="Dotted path, e.g. llm.model / llm.provider / version")
    p_get.set_defaults(func=cmd_config)

    p_set = sub.add_parser(
        "set", help="Write an llm config key (e.g. config set llm.model gpt-4o)")
    add_shared_flags(p_set, inherited=True)
    p_set.add_argument("key", help="Dotted path; currently supports llm.<field>")
    p_set.add_argument("value", help="New value (numbers / booleans are normalized by field type)")
    p_set.set_defaults(func=cmd_config)

    parser.set_defaults(func=group_dispatch(parser, cmd_config, action_dest="config_action"))
