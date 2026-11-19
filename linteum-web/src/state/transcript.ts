import { MAX_INLINE_OUTPUT_CHARS, MAX_TRANSCRIPT_ITEMS } from "../config";
import type { SubagentInfo, ViewFrame } from "../protocol/frames";
import type {
  AssistantItem,
  CompactionItem,
  ErrorItem,
  ItemBase,
  NoticeItem,
  SubagentItem,
  ToolItem,
  ToolStatus,
  TranscriptItem,
  UserItem,
} from "./types";

type UserFrame = Extract<ViewFrame, { kind: "user.message" }>;
type StartedFrame = Extract<ViewFrame, { kind: "assistant.started" }>;
type MessageFrame = Extract<ViewFrame, { kind: "assistant.message" }>;
type ToolFrame = Extract<
  ViewFrame,
  { kind: "tool.requested" | "tool.started" | "tool.delta" | "tool.result" }
>;
type NoticeFrame = Extract<ViewFrame, { kind: "notice" }>;
type ErrorFrame = Extract<ViewFrame, { kind: "error" }>;
type CompactionFrame = Extract<
  ViewFrame,
  { kind: "compaction.started" | "compaction.applied" }
>;

function base(frame: ViewFrame, id: string): ItemBase {
  return { id, seq: frame.seq, ordinal: frame.ordinal, createdAt: 0 };
}

function replaceAt<T>(items: readonly T[], index: number, item: T): T[] {
  const copy = items.slice();
  copy[index] = item;
  return copy;
}

function cap(items: TranscriptItem[]): TranscriptItem[] {
  if (items.length <= MAX_TRANSCRIPT_ITEMS) return items;
  return items.slice(items.length - MAX_TRANSCRIPT_ITEMS);
}

export function appendUser(items: TranscriptItem[], frame: UserFrame): TranscriptItem[] {
  if (!frame.text && !frame.clientMessageId) return items;
  const item: UserItem = {
    ...base(frame, `user-${frame.seq}-${frame.ordinal}`),
    kind: "user",
    text: frame.text,
    clientMessageId: frame.clientMessageId,
    eventId: frame.eventId,
    traceId: frame.traceId,
  };
  return cap([...items, item]);
}

export function startAssistant(items: TranscriptItem[], frame: StartedFrame): TranscriptItem[] {
  const item: AssistantItem = {
    ...base(frame, `assistant-${frame.seq}-${frame.ordinal}`),
    kind: "assistant",
    text: "",
    reasoning: "",
    streaming: true,
    interrupted: false,
    provider: frame.provider,
    model: frame.model,
    usage: {},
    cost: {},
    toolCalls: [],
  };
  return cap([...items, item]);
}

function lastStreamingAssistantIndex(items: TranscriptItem[]): number {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.kind === "assistant" && item.streaming) return index;
  }
  return -1;
}

export function appendAssistantDelta(
  items: TranscriptItem[],
  kind: "text" | "reasoning",
  text: string,
): TranscriptItem[] {
  if (!text) return items;
  const index = lastStreamingAssistantIndex(items);
  if (index < 0) {
    const item: AssistantItem = {
      kind: "assistant",
      id: `assistant-live-${items.length}`,
      seq: 0,
      ordinal: 0,
      createdAt: 0,
      text: kind === "text" ? text : "",
      reasoning: kind === "reasoning" ? text : "",
      streaming: true,
      interrupted: false,
      usage: {},
      cost: {},
      toolCalls: [],
    };
    return cap([...items, item]);
  }
  const current = items[index] as AssistantItem;
  const updated: AssistantItem =
    kind === "text"
      ? { ...current, text: current.text + text }
      : { ...current, reasoning: current.reasoning + text };
  return replaceAt(items, index, updated);
}

export function finalizeAssistant(items: TranscriptItem[], frame: MessageFrame): TranscriptItem[] {
  const index = lastStreamingAssistantIndex(items);
  if (index < 0) {
    const item: AssistantItem = {
      ...base(frame, `assistant-${frame.seq}-${frame.ordinal}`),
      kind: "assistant",
      text: frame.text,
      reasoning: frame.reasoning ?? "",
      streaming: false,
      interrupted: frame.interrupted,
      provider: frame.provider,
      model: frame.model,
      usage: frame.usage,
      cost: frame.cost,
      finish: frame.finish,
      toolCalls: frame.toolCalls,
    };
    return cap([...items, item]);
  }
  const current = items[index] as AssistantItem;
  const updated: AssistantItem = {
    ...current,
    text: frame.text || current.text,
    reasoning: frame.reasoning ?? current.reasoning,
    streaming: false,
    interrupted: frame.interrupted,
    provider: frame.provider ?? current.provider,
    model: frame.model ?? current.model,
    usage: frame.usage,
    cost: frame.cost,
    finish: frame.finish,
    toolCalls: frame.toolCalls,
  };
  return replaceAt(items, index, updated);
}

function findToolIndex(items: TranscriptItem[], callId: string): number {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.kind === "tool" && item.callId === callId) return index;
  }
  return -1;
}

function toolStatus(frame: Extract<ViewFrame, { kind: "tool.result" }>): ToolStatus {
  if (!frame.background) return frame.ok ? "ok" : "failed";
  if (frame.backgroundStatus === "failed") return "failed";
  if (frame.backgroundStatus === "queued") return "queued";
  return "backgrounded";
}

export function upsertTool(items: TranscriptItem[], frame: ToolFrame): TranscriptItem[] {
  if (!frame.callId) return items;
  const index = findToolIndex(items, frame.callId);

  if (frame.kind === "tool.requested") {
    if (index >= 0) return items;
    const item: ToolItem = {
      ...base(frame, `tool-${frame.callId}`),
      kind: "tool",
      callId: frame.callId,
      name: frame.name,
      args: frame.args,
      status: "requested",
    };
    return cap([...items, item]);
  }

  if (frame.kind === "tool.started") {
    if (index < 0) {
      const item: ToolItem = {
        ...base(frame, `tool-${frame.callId}`),
        kind: "tool",
        callId: frame.callId,
        name: frame.name ?? "tool",
        status: frame.background ? "backgrounded" : "running",
        sideEffect: frame.sideEffect,
        background: frame.background,
      };
      return cap([...items, item]);
    }
    const current = items[index] as ToolItem;
    const updated: ToolItem = {
      ...current,
      name: frame.name ?? current.name,
      status: frame.background ? "backgrounded" : "running",
      sideEffect: frame.sideEffect ?? current.sideEffect,
      background: current.background || frame.background,
      startedAt: current.startedAt,
    };
    return replaceAt(items, index, updated);
  }

  if (frame.kind === "tool.delta") {
    if (index < 0) {
      const item: ToolItem = {
        ...base(frame, `tool-${frame.callId}`),
        kind: "tool",
        callId: frame.callId,
        name: "tool",
        status: frame.background ? "backgrounded" : "running",
        background: frame.background,
        taskId: frame.backgroundTaskId ?? null,
        output: frame.text.slice(0, MAX_INLINE_OUTPUT_CHARS),
      };
      return cap([...items, item]);
    }
    const current = items[index] as ToolItem;
    const previous = typeof current.output === "string" ? current.output : "";
    const output = (previous + frame.text).slice(0, MAX_INLINE_OUTPUT_CHARS);
    const updated: ToolItem = {
      ...current,
      status: current.status === "requested" ? "running" : current.status,
      output,
      background: current.background || frame.background,
      taskId: current.taskId ?? frame.backgroundTaskId ?? null,
    };
    return replaceAt(items, index, updated);
  }

  const status = toolStatus(frame);
  if (index < 0) {
    const item: ToolItem = {
      ...base(frame, `tool-${frame.callId}`),
      kind: "tool",
      callId: frame.callId,
      name: frame.name ?? "tool",
      status,
      output: frame.output,
      outputRef: frame.outputRef,
      code: frame.code,
      error: frame.error,
      errorInfo: frame.errorInfo,
      background: frame.background,
      taskId: frame.taskId,
    };
    return cap([...items, item]);
  }
  const current = items[index] as ToolItem;
  const updated: ToolItem = {
    ...current,
    name: frame.name ?? current.name,
    status,
    output: frame.output !== undefined ? frame.output : current.output,
    outputRef: frame.outputRef ?? current.outputRef,
    code: frame.code ?? current.code,
    error: frame.error ?? current.error,
    errorInfo: frame.errorInfo ?? current.errorInfo,
    background: current.background || frame.background,
    taskId: frame.taskId ?? current.taskId,
  };
  return replaceAt(items, index, updated);
}

export function upsertSubagentItem(
  items: TranscriptItem[],
  callId: string,
  update: (current: SubagentInfo | undefined) => SubagentInfo,
): TranscriptItem[] {
  if (!callId) return items;
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.kind === "subagent" && item.subagent.callId === callId) {
      const current = item as SubagentItem;
      return replaceAt(items, index, {
        ...current,
        subagent: update(current.subagent),
      });
    }
  }
  const item: SubagentItem = {
    kind: "subagent",
    id: `subagent-${callId}`,
    seq: 0,
    ordinal: 0,
    createdAt: 0,
    subagent: update(undefined),
  };
  return cap([...items, item]);
}

export function upsertCompaction(
  items: TranscriptItem[],
  frame: CompactionFrame,
): TranscriptItem[] {
  if (frame.kind === "compaction.started") {
    const item: CompactionItem = {
      ...base(frame, `compaction-${frame.seq}-${frame.ordinal}`),
      kind: "compaction",
      status: "started",
      fromIndex: frame.fromIndex,
      toIndex: frame.toIndex,
    };
    return cap([...items, item]);
  }
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.kind === "compaction" && item.status === "started") {
      const current = item as CompactionItem;
      return replaceAt(items, index, {
        ...current,
        status: "applied",
        fromIndex: frame.fromIndex ?? current.fromIndex,
        toIndex: frame.toIndex ?? current.toIndex,
        summary: frame.summary,
        summaryRef: frame.summaryRef,
        tokens: frame.tokens,
        fallback: frame.fallback,
        errorInfo: frame.errorInfo,
      });
    }
  }
  const item: CompactionItem = {
    ...base(frame, `compaction-${frame.seq}-${frame.ordinal}`),
    kind: "compaction",
    status: "applied",
    fromIndex: frame.fromIndex,
    toIndex: frame.toIndex,
    summary: frame.summary,
    summaryRef: frame.summaryRef,
    tokens: frame.tokens,
    fallback: frame.fallback,
    errorInfo: frame.errorInfo,
  };
  return cap([...items, item]);
}

export function appendNotice(items: TranscriptItem[], frame: NoticeFrame): TranscriptItem[] {
  const item: NoticeItem = {
    ...base(frame, `notice-${frame.seq}-${frame.ordinal}-${frame.noticeKind}`),
    kind: "notice",
    noticeKind: frame.noticeKind,
    payload: frame.payload,
  };
  return cap([...items, item]);
}

export function appendError(items: TranscriptItem[], frame: ErrorFrame): TranscriptItem[] {
  const item: ErrorItem = {
    ...base(frame, `error-${frame.seq}-${frame.ordinal}`),
    kind: "error",
    errorInfo: frame.errorInfo,
    code: frame.code,
    round: frame.round,
  };
  return cap([...items, item]);
}
