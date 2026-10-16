"""DeepSeek adapter (§38.3) — mirrors ``linteum_agent/model/llm.py``.

DeepSeek is OpenAI-compatible, so it rides the ``AsyncOpenAI`` client pointed at
``https://api.deepseek.com`` and overrides only the differences:

* **api key** — from ``DEEPSEEK_API_KEY`` (via ``config.resolved_api_key()``);
* **thinking mode** — :attr:`LLMConfig.thinking` / :attr:`LLMConfig.reasoning_effort`
  are translated per dialect:

  ===========  ==================================================================
   dialect      thinking switch + effort
  ===========  ==================================================================
   chat         ``reasoning_effort`` + ``extra_body.thinking={"type":"enabled"|"disabled"}``
   responses    ``reasoning={"effort":"none"|"low"|"high"|"max"}``
                (``thinking=False`` → ``none``; default effort ``high``)
  ===========  ==================================================================

* **reasoning echo** — assistant ``reasoning_content`` is sent back so a
  "chain-of-thought + tool calling" conversation can continue (hence the
  capability policy is ``keep``, not ``strip``);
* **usage** — DeepSeek reports cache hits under different names (D5) and needs
  ``stream_options.include_usage`` to report usage while streaming;
* **models** — ``deepseek-flash`` / ``deepseek-v4-pro`` (both support thinking).
"""

from __future__ import annotations

from typing import Optional

from .base import field
from .openai import OpenAIAdapter
from ..canonical import Capabilities, Usage

#: Default effort used when thinking is enabled but no explicit effort is chosen.
DEFAULT_THINKING_EFFORT = "high"


def _deepseek_default_caps() -> Capabilities:
    """One chat-like baseline for every DeepSeek model id (no per-model table).

    Both ``deepseek-flash`` and ``deepseek-v4-pro`` support thinking mode and
    tools; per-model differences are the host's call via ``capabilities_map=``.
    """
    return Capabilities(
        ctx_window=128_000,
        # generic / vendor-routed ids -> the real output ceiling is unknown, so do
        # NOT hard-code it: ``0`` means ``LLMExecutor._clamp`` does not clamp.
        max_output=0,
        tools=True,
        reasoning=True,                       # thinking is supported
        cache_mode="auto",
        stream_usage=True,
        reasoning_history_policy="keep",       # reasoning_content is echoed back
        params_supported=frozenset({"temperature", "top_p", "stop"}),
    )


class DeepSeekAdapter(OpenAIAdapter):
    provider = "deepseek"
    adapter_version = "2.2.0"

    def __init__(
        self,
        config=None,
        *,
        client=None,
        capabilities_map: Optional[dict] = None,
        default_capabilities: Optional[Capabilities] = None,
        pricing: Optional[dict] = None,
    ) -> None:
        # api key comes from DEEPSEEK_API_KEY (via config.resolved_api_key())
        super().__init__(
            config,
            client=client,
            capabilities_map=capabilities_map,
            default_capabilities=default_capabilities or _deepseek_default_caps(),
            pricing=pricing,
        )

    # -- usage: DeepSeek reports cache hits under a different name (D5) -----
    def _chat_usage(self, usage) -> Usage:
        if usage is None:
            return Usage(0, 0, estimated=True)
        return Usage(
            input_tokens=int(field(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(field(usage, "completion_tokens", 0) or 0),
            cache_read=int(field(usage, "prompt_cache_hit_tokens", 0) or 0),
            cache_write=int(field(usage, "prompt_cache_miss_tokens", 0) or 0),
            reasoning_tokens=int(
                field(field(usage, "completion_tokens_details"), "reasoning_tokens", 0) or 0
            ),
        )

    # -- request: thinking parameters + reasoning echo + usage ---------------
    def build_body(self, req) -> tuple:
        body, dropped = super().build_body(req)

        if req.stream:  # DeepSeek needs include_usage to report usage while streaming
            stream_options = dict(body.get("stream_options") or {})
            stream_options.setdefault("include_usage", True)
            body["stream_options"] = stream_options

        if self.mode == "responses":
            self._apply_thinking_responses(body)
        else:
            self._apply_thinking_chat(body)
            self._echo_reasoning(body, req)
        return body, dropped

    # -- thinking parameter mapping (mirrors the reference) ------------------
    def _apply_thinking_chat(self, body: dict) -> None:
        config = self.config
        if config is None:
            return
        if config.reasoning_effort is not None:
            body.setdefault("reasoning_effort", config.reasoning_effort)
        if config.thinking is not None:
            extra_body = dict(body.get("extra_body") or {})
            extra_body.setdefault(
                "thinking",
                {"type": "enabled" if config.thinking else "disabled"},
            )
            body["extra_body"] = extra_body

    def _apply_thinking_responses(self, body: dict) -> None:
        config = self.config
        if config is None:
            return
        effort = config.reasoning_effort
        enabled = config.thinking
        if effort is not None or enabled is not None:
            if enabled is False:
                effort = "none"
            body.setdefault("reasoning", {"effort": effort or DEFAULT_THINKING_EFFORT})

    @staticmethod
    def _echo_reasoning(body: dict, req) -> None:
        """Send assistant ``reasoning_content`` back (DeepSeek requires it).

        The base encoder drops reasoning (OpenAI never replays it); DeepSeek's
        multi-turn "thinking + tools" flow needs it, so we re-attach it here.
        """
        messages = body.get("messages")
        if not messages:
            return
        for original, encoded in zip(req.messages, messages):
            reasoning = getattr(original, "reasoning", None)
            if encoded.get("role") == "assistant" and reasoning:
                encoded.setdefault("reasoning_content", reasoning)
