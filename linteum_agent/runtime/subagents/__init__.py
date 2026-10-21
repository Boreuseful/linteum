"""Subagent delegation layer (design ``subagent-design-v0.1``).

The root :class:`~linteum_agent.runtime.agent.Agent` still sees an ordinary tool
schema; this package owns the *effect* side:

* :mod:`.catalog` — reserved schema + host-bound policy resolution;
* :mod:`.summary` — bounded summary extraction / artifact externalisation;
* :mod:`.orchestrator` — the sole ``subagent.spawn.requested`` subscriber.

Child sessions are assembled by :class:`~linteum_agent.runtime.host.AgentHost`;
the orchestrator never touches the parent ``AgentState`` directly.
"""

from __future__ import annotations

from ...config.schema import SubagentSettings
from .catalog import SubagentCatalog
from .errors import SubagentError
from .model import SubagentPolicy, SubagentResult, SubagentTask
from .orchestrator import SubagentOrchestrator
from .summary import build_summary, extract_summary, truncate_utf8

# NOTE: the host imports ``SubagentOrchestrator`` lazily inside
# ``_ensure_orchestrator`` so a host <-> subagents cycle cannot form at module
# import time; the class itself does not import ``runtime.host``.
__all__ = [
    "SubagentCatalog",
    "SubagentError",
    "SubagentOrchestrator",
    "SubagentPolicy",
    "SubagentResult",
    "SubagentSettings",
    "SubagentTask",
    "build_summary",
    "extract_summary",
    "truncate_utf8",
]
