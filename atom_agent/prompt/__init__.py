"""Prompt 层：系统提示词模板的加载与预填充。

对外入口：

- ``build_system_prompt()`` / ``build_system_prompt_parts()``：加载模板并填充
  ``<tools_list>`` / ``<skills_list>`` / ``<system_env>`` 三个占位符；
- ``load_default_template()`` / ``load_template()`` / ``template_path()`` /
  ``templates_dir()``：直接读取模板；
- ``render_tools()`` / ``render_skills()`` / ``collect_system_env()``：单独渲染各区块。

参见 ``prompt`` 模块文档了解占位符与输入格式。
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
