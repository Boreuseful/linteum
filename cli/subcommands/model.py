"""Parser for the ``linteum model`` subcommand."""

from __future__ import annotations

from typing import Callable

from cli.subcommands._shared import add_shared_flags, group_dispatch


def build_model_parser(subparsers, *, cmd_model: Callable) -> None:
    """Attach the ``model`` subcommand (including show / set) to ``subparsers``."""
    parser = subparsers.add_parser(
        "model", help="View / switch the default model",
        description="Read and write llm.model (and llm.provider) in config.yaml.")
    sub = parser.add_subparsers(dest="model_action")

    p_show = sub.add_parser("show", help="Show the current provider and model")
    add_shared_flags(p_show, inherited=True)
    p_show.set_defaults(func=cmd_model)

    p_set = sub.add_parser("set", help="Set the default model")
    add_shared_flags(p_set, inherited=True)
    p_set.add_argument("name", help="Model name, e.g. gpt-4o")
    p_set.add_argument("--provider", default=None, help="Also switch the provider (e.g. deepseek)")
    p_set.set_defaults(func=cmd_model)

    parser.set_defaults(func=group_dispatch(parser, cmd_model, action_dest="model_action"))
