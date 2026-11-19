import { asRecord, asString } from "../../protocol/envelope";

export interface ContextMessageView {
  role: string;
  text: string;
}

/**
 * Flatten one provider-facing context message into displayable text.
 *
 * The context plan contains system blocks, plain text, reasoning, tool calls
 * and paired tool results. Showing all of them (not a preview) is what makes
 * the child-context viewer complete.
 */
export function formatContextMessage(value: unknown): ContextMessageView {
  const message = asRecord(value);
  const role = asString(message.role) ?? "unknown";
  const parts: string[] = [];

  const toolCallId = asString(message.tool_call_id);
  if (toolCallId) parts.push(`[tool_result ${toolCallId}]`);

  const text = contentText(message.content);
  if (text) parts.push(text);

  const reasoning = asString(message.reasoning);
  if (reasoning) parts.push(`[reasoning] ${reasoning}`);

  for (const raw of Array.isArray(message.tool_calls) ? message.tool_calls : []) {
    const call = asRecord(raw);
    const fn = asRecord(call.function);
    const name = asString(call.name) ?? asString(fn.name) ?? "tool";
    const args = call.args ?? call.arguments ?? fn.arguments;
    const rendered = formatArguments(args);
    parts.push(`[tool_call] ${name}${rendered ? ` ${rendered}` : ""}`);
  }

  return { role, text: parts.join("\n") };
}

function formatArguments(args: unknown): string {
  if (args === undefined || args === null) return "";
  // Provider messages may already carry arguments as a JSON string.
  if (typeof args === "string") return args;
  return JSON.stringify(args) ?? String(args);
}

function contentText(content: unknown): string {
  if (typeof content === "string") return content;
  if (content === null || content === undefined) return "";
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === "string") return part;
        const record = asRecord(part);
        if (asString(record.type) === "text") {
          return asString(record.text) ?? "";
        }
        return JSON.stringify(part, null, 2) ?? String(part);
      })
      .filter((part) => part !== "")
      .join("\n");
  }
  return JSON.stringify(content, null, 2) ?? String(content);
}
