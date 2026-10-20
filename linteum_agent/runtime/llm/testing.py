"""Offline SDK fakes for tests and replay (§45.1).

Adapters talk to the real ``AsyncOpenAI`` / ``AsyncAnthropic`` clients; these
fakes expose *exactly* the call shapes the adapters use, so the whole adapter
layer can be exercised with recorded/scripted responses and no network.

Used call shapes (mirroring ``linteum_agent``):

* ``await client.chat.completions.create(**body)``   -> response | async-iterable
* ``await client.responses.create(**body)``          -> response | async-iterable
* ``await client.messages.create(**body)``           -> message
* ``client.messages.stream(**body)``                 -> async context manager
      with ``.text_stream`` and ``await .get_final_message()``
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterable, Optional


async def _aiter(items: Iterable[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item


class _Scripted:
    """A FIFO of scripted outcomes shared by the fake clients."""

    def __init__(self) -> None:
        self._queue: deque = deque()
        self.calls: list = []

    def push(self, *, stream: Optional[list] = None, response: Any = None,
             error: Optional[BaseException] = None) -> "_Scripted":
        self._queue.append({"stream": stream, "response": response, "error": error})
        return self

    def push_error(self, error: BaseException) -> "_Scripted":
        return self.push(error=error)

    def _pop(self):
        if not self._queue:
            raise AssertionError("fake client exhausted: no scripted response left")
        return self._queue.popleft()


# ---------------------------------------------------------------------------
# OpenAI / OpenAI-compatible
# ---------------------------------------------------------------------------

class _FakeChatCompletions:
    def __init__(self, owner: "_Scripted") -> None:
        self._o = owner

    async def create(self, **body: Any):
        self._o.calls.append({"api": "chat.completions", "body": body})
        item = self._o._pop()
        if item["error"] is not None:
            raise item["error"]
        if body.get("stream"):
            return _aiter(item["stream"] or [])
        return item["response"]


class _FakeResponses:
    def __init__(self, owner: "_Scripted") -> None:
        self._o = owner

    async def create(self, **body: Any):
        self._o.calls.append({"api": "responses", "body": body})
        item = self._o._pop()
        if item["error"] is not None:
            raise item["error"]
        if body.get("stream"):
            return _aiter(item["stream"] or [])
        return item["response"]


class FakeOpenAIClient(_Scripted):
    """Stand-in for ``openai.AsyncOpenAI`` (and any compatible endpoint)."""

    def __init__(self) -> None:
        super().__init__()
        self.chat = SimpleNamespace(completions=_FakeChatCompletions(self))
        self.responses = _FakeResponses(self)

    @property
    def last_body(self) -> dict:
        return self.calls[-1]["body"]


# ---------------------------------------------------------------------------
# Object builders (attribute access, like the real SDK objects)
# ---------------------------------------------------------------------------

def openai_usage(prompt_tokens: int, completion_tokens: int, cached: int = 0):
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


def openai_tool_delta(index: int, *, id: Optional[str] = None, name: Optional[str] = None,
                      arguments: Optional[str] = None):
    return SimpleNamespace(
        index=index,
        id=id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def openai_chunk(*, content: Optional[str] = None, reasoning: Optional[str] = None,
                 tool_calls: Optional[list] = None, usage=None, model: Optional[str] = None,
                 finish: Optional[str] = None):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=finish)
    return SimpleNamespace(choices=[choice], usage=usage, model=model)


def openai_chunk_usage_only(usage, *, model: Optional[str] = None):
    return SimpleNamespace(choices=[], usage=usage, model=model)


def openai_tool_call(id: str, name: str, arguments_json: str):
    return SimpleNamespace(id=id, function=SimpleNamespace(name=name, arguments=arguments_json))


def openai_chat_response(text: str = "", *, tool_calls: tuple = (), usage=None, model: str = "m"):
    message = SimpleNamespace(content=text, tool_calls=list(tool_calls), reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage, model=model)


def responses_usage(input_tokens: int = 0, output_tokens: int = 0, cached: int = 0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


def responses_function_call(call_id: str, name: str, arguments_json: str):
    return SimpleNamespace(type="function_call", call_id=call_id, name=name, arguments=arguments_json)


def responses_reasoning_item(text: str):
    return SimpleNamespace(type="reasoning", reasoning_text=text, content=None, summary=None)


def responses_response(text: str = "", *, tool_calls: tuple = (), usage=None, model: str = "m",
                       reasoning: Optional[str] = None, response_id: str = "resp_1"):
    output = []
    if reasoning:
        output.append(responses_reasoning_item(reasoning))
    output.extend(tool_calls)
    return SimpleNamespace(model=model, usage=usage, output=output, output_text=text, id=response_id)


def responses_event(type: str, **kw):
    return SimpleNamespace(type=type, **kw)


# ---------------------------------------------------------------------------
# Anthropic builders
# ---------------------------------------------------------------------------

def anthropic_usage(input_tokens: int = 0, output_tokens: int = 0,
                    cache_read: int = 0, cache_write: int = 0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


def anthropic_tool_use(id: str, name: str, input_dict: dict):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input_dict)


def anthropic_thinking(text: str, signature: str = "sig"):
    return SimpleNamespace(type="thinking", thinking=text, signature=signature)


def anthropic_message(*, text: str = "", tool_calls: tuple = (), thinking: tuple = (),
                      usage=None, model: str = "claude", stop_reason: str = "end_turn"):
    content: list = []
    content.extend(thinking)
    if text:
        content.append(SimpleNamespace(type="text", text=text))
    content.extend(tool_calls)
    return SimpleNamespace(content=content, usage=usage, model=model, stop_reason=stop_reason)


class _FakeMessageStream:
    def __init__(self, item: dict) -> None:
        self._item = item

    async def __aenter__(self):
        if self._item["error"] is not None:
            raise self._item["error"]
        message = self._item["response"]
        texts = [b.text for b in getattr(message, "content", []) if getattr(b, "type", None) == "text"]
        fake = SimpleNamespace()
        fake.text_stream = _aiter(texts)
        async def get_final_message():
            return message
        fake.get_final_message = get_final_message
        return fake

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeMessages:
    def __init__(self, owner: "_Scripted") -> None:
        self._o = owner

    async def create(self, **body: Any):
        self._o.calls.append({"api": "messages.create", "body": body})
        item = self._o._pop()
        if item["error"] is not None:
            raise item["error"]
        return item["response"]

    def stream(self, **body: Any):
        self._o.calls.append({"api": "messages.stream", "body": body})
        return _FakeMessageStream(self._o._pop())


class FakeAnthropicClient(_Scripted):
    """Stand-in for ``anthropic.AsyncAnthropic``."""

    def __init__(self) -> None:
        super().__init__()
        self.messages = _FakeMessages(self)

    @property
    def last_body(self) -> dict:
        return self.calls[-1]["body"]
