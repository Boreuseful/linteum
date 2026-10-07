"""Provider-neutral, class-based LLM messages with JSON compatibility."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from collections.abc import Iterable
from typing import Any, Literal, TypeAlias


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] | str = field(default_factory=dict)

    def arguments_json(self) -> str:
        return self.arguments if isinstance(self.arguments, str) else json.dumps(self.arguments, ensure_ascii=False)


@dataclass
class ToolDefinition:
    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_openai(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}

    def to_anthropic(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}

    def to_responses(self) -> dict[str, Any]:
        return {"type": "function", "name": self.name, "description": self.description, "parameters": self.parameters}


@dataclass
class SystemPrompt:
    content: str
    role: Literal["system"] = "system"


@dataclass
class UserPrompt:
    content: str | list[dict[str, Any]]
    name: str | None = None
    role: Literal["user"] = "user"


@dataclass
class AssistantMessage:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Chain-of-thought content (DeepSeek reasoning_content).
    # When a request carries tools, the reasoning_content of previous rounds must be sent back too.
    reasoning_content: str | None = None
    role: Literal["assistant"] = "assistant"

@dataclass
class ToolMessage:
    role: Literal["tool"]
    tool_call_id: str
    content: Any

@dataclass
class ToolResult:
    tool_call_id: str
    tool_name: str
    content: str | list[dict[str, Any]]
    is_error: bool = False
    details: dict[str, Any] | None = None
    role: Literal["tool"] = "tool"

@dataclass
class GenericMessage:
    role: str
    content: Any = ""
    extra: dict[str, Any] = field(default_factory=dict)



Message: TypeAlias = SystemPrompt | UserPrompt | AssistantMessage | ToolResult | GenericMessage
MessageInput: TypeAlias = Message | dict[str, Any]
MessageInputs: TypeAlias = MessageInput | Iterable[MessageInput]


def _tool_call_from_dict(value: dict[str, Any]) -> ToolCall:
    function = value.get("function", value)
    arguments: Any = function.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            pass
    return ToolCall(id=value.get("id", ""), name=function.get("name", ""), arguments=arguments)


def from_dict(message: dict[str, Any]) -> Message:
    role = message.get("role")
    content = message.get("content", "")
    if role == "system":
        return SystemPrompt(content=content)
    if role == "user":
        return UserPrompt(content=content, name=message.get("name"))
    if role == "assistant":
        return AssistantMessage(
            content=content,
            tool_calls=[_tool_call_from_dict(item) for item in message.get("tool_calls", [])],
            reasoning_content=message.get("reasoning_content"),
        )
    if role == "tool":
        return ToolResult(
            tool_call_id=message.get("tool_call_id", ""),
            tool_name=message.get("tool_name", ""),
            content=content,
            is_error=message.get("is_error", False),
            details=message.get("details"),
        )
    if not role:
        raise ValueError("every message must contain a role")
    return GenericMessage(role=role, content=content, extra={k: v for k, v in message.items() if k not in {"role", "content"}})


def normalize_messages(messages: MessageInputs) -> list[Message]:
    """Normalize a single message or a collection of messages into a list of typed messages."""
    if isinstance(messages, dict) or hasattr(messages, "role"):
        values = [messages]
    else:
        values = list(messages)
    messages = [from_dict(item) if isinstance(item, dict) else item for item in values]
    # Backward compatibility with old context: the tool message in old JSON did not save tool_name,
    # which can be recovered from the corresponding assistant tool call.
    for index, message in enumerate(messages):
        if not isinstance(message, ToolResult) or message.tool_name:
            continue
        for previous in reversed(messages[:index]):
            if isinstance(previous, AssistantMessage):
                for call in previous.tool_calls:
                    if call.id == message.tool_call_id:
                        message.tool_name = call.name
                        break
                if message.tool_name:
                    break
    return messages


def to_openai_message(message: Message) -> dict[str, Any]:
    if isinstance(message, SystemPrompt):
        return {"role": "system", "content": message.content}
    if isinstance(message, UserPrompt):
        result: dict[str, Any] = {"role": "user", "content": message.content}
        if message.name:
            result["name"] = message.name
        return result
    if isinstance(message, AssistantMessage):
        result: dict[str, Any] = {"role": "assistant", "content": message.content}
        if message.reasoning_content:
            # DeepSeek requires requests carrying tools to send back the historical reasoning_content
            # so the model can continue the previous round's chain of thought.
            result["reasoning_content"] = message.reasoning_content
        if message.tool_calls:
            result["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments_json()}} for c in message.tool_calls]
        return result
    if isinstance(message, ToolResult):
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
    result = {"role": message.role, "content": message.content}
    result.update(message.extra)
    return result


def to_responses_input(message: Message) -> list[dict[str, Any]]:
    """Convert the message into OpenAI Responses API input item(s)."""
    if isinstance(message, AssistantMessage):
        items: list[dict[str, Any]] = []
        if message.reasoning_content:
            # Under the Responses API the chain of thought is sent back as a reasoning item,
            # and DeepSeek merges its plain-text content into the adjacent assistant message.
            items.append(
                {
                    "type": "reasoning",
                    "content": [
                        {"type": "reasoning_text", "text": message.reasoning_content}
                    ],
                }
            )
        if message.content:
            items.append({"role": "assistant", "content": message.content})
        items.extend(
            {
                "type": "function_call",
                "call_id": call.id,
                "name": call.name,
                "arguments": call.arguments_json(),
            }
            for call in message.tool_calls
        )
        return items
    if isinstance(message, ToolResult):
        return [
            {
                "type": "function_call_output",
                "call_id": message.tool_call_id,
                "output": message.content,
            }
        ]
    return [to_openai_message(message)]


def to_anthropic_message(message: Message) -> dict[str, Any] | None:
    if isinstance(message, UserPrompt):
        return {"role": "user", "content": message.content}
    if isinstance(message, AssistantMessage):
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        blocks.extend({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments if isinstance(c.arguments, dict) else {}} for c in message.tool_calls)
        return {"role": "assistant", "content": blocks}
    if isinstance(message, ToolResult):
        return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": message.tool_call_id, "content": message.content}]}
    if isinstance(message, GenericMessage):
        return {"role": message.role, "content": message.content, **message.extra}
    return None


def to_dict(message: Message) -> dict[str, Any]:
    """Convert a typed message into a stable, JSON-serializable internal format."""
    if isinstance(message, SystemPrompt):
        return {"role": "system", "content": message.content}
    if isinstance(message, UserPrompt):
        result: dict[str, Any] = {"role": "user", "content": message.content}
        if message.name is not None:
            result["name"] = message.name
        return result
    if isinstance(message, AssistantMessage):
        result: dict[str, Any] = {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments_json(),
                    },
                }
                for call in message.tool_calls
            ],
        }
        if message.reasoning_content:
            result["reasoning_content"] = message.reasoning_content
        return result
    if isinstance(message, ToolResult):
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "tool_name": message.tool_name,
            "content": message.content,
            "is_error": message.is_error,
            "details": message.details,
        }
    result = {"role": message.role, "content": message.content}
    result.update(message.extra)
    return result


# Short aliases matching the class-style API used by applications.
userprompt = UserPrompt
systemprompt = SystemPrompt
assistantmessage = AssistantMessage
toolcall = ToolCall
toolresult = ToolResult
tooldefinition = ToolDefinition