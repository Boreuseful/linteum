import { describe, expect, it } from "vitest";
import { formatContextMessage } from "../src/ui/overlays/contextMessage";

describe("formatContextMessage", () => {
  it("renders string content", () => {
    expect(formatContextMessage({ role: "user", content: "hello" })).toEqual({
      role: "user",
      text: "hello",
    });
  });

  it("joins text parts of a content array", () => {
    const view = formatContextMessage({
      role: "assistant",
      content: [{ type: "text", text: "one" }, { type: "text", text: "two" }],
    });
    expect(view.text).toBe("one\ntwo");
  });

  it("includes reasoning, tool calls and paired tool results", () => {
    const view = formatContextMessage({
      role: "assistant",
      content: "call",
      reasoning: "because",
      tool_calls: [
        { name: "bash", args: { cmd: "ls" } },
        { function: { name: "read", arguments: '{"path":"a"}' } },
      ],
      tool_call_id: "call-1",
    });
    expect(view.text).toContain("[tool_result call-1]");
    expect(view.text).toContain("call");
    expect(view.text).toContain("[reasoning] because");
    expect(view.text).toContain('[tool_call] bash {"cmd":"ls"}');
    expect(view.text).toContain('[tool_call] read {"path":"a"}');
  });

  it("never drops non-text content", () => {
    const view = formatContextMessage({
      role: "user",
      content: [{ type: "image", url: "art:x" }],
    });
    expect(view.text).toContain('"type": "image"');
    expect(view.text).toContain('"url": "art:x"');
  });

  it("falls back to unknown for malformed messages", () => {
    expect(formatContextMessage(null)).toEqual({ role: "unknown", text: "" });
    expect(formatContextMessage({ content: 42 }).text).toBe("42");
  });
});
