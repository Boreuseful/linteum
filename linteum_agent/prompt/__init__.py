"""Prompt layer: loading and pre-filling the system prompt template.

Public entry points:

- ``build_system_prompt()`` / ``build_system_prompt_parts()``: load the template and fill the three
  placeholders ``<tools_list>`` / ``<skills_list>`` / ``<system_env>``;
- ``load_default_template()`` / ``load_template()`` / ``template_path()`` / ``templates_dir()``:
  read the template directly;
- ``render_tools()`` / ``render_skills()`` / ``collect_system_env()``: render each section
  separately.

See the ``prompt`` module documentation for the placeholders and input format.
"""

from .prompt import (
    DEFAULT_TEMPLATE_FILENAME,
    EMPTY_SKILLS_TEXT,
    EMPTY_TOOLS_TEXT,
    PROMPT_DIRNAME,
    SKILLS_PLACEHOLDER,
    SYSTEM_ENV_PLACEHOLDER,
    TOOLS_PLACEHOLDER,
    build_system_prompt,
    build_system_prompt_parts,
    collect_system_env,
    fill_placeholders,
    load_default_template,
    load_template,
    render_skills,
    render_tools,
    template_path,
    templates_dir,
)

__all__ = [
    "PROMPT_DIRNAME",
    "DEFAULT_TEMPLATE_FILENAME",
    "TOOLS_PLACEHOLDER",
    "SKILLS_PLACEHOLDER",
    "SYSTEM_ENV_PLACEHOLDER",
    "EMPTY_TOOLS_TEXT",
    "EMPTY_SKILLS_TEXT",
    "templates_dir",
    "template_path",
    "load_default_template",
    "load_template",
    "collect_system_env",
    "render_tools",
    "render_skills",
    "fill_placeholders",
    "build_system_prompt_parts",
    "build_system_prompt",
]
