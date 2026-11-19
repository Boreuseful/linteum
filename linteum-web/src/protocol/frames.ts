/**
 * Gateway frame → domain event.
 *
 * This is the only place that interprets wire payloads. Unknown frames become
 * `null` and are ignored by the reducer (forward compatibility).
 */

import {
  asArray,
  asBoolean,
  asNumber,
  asRecord,
  asString,
  asStringArray,
  asErrorInfo,
  isRecord,
  type ErrorInfo,
  type FrameEnvelope,
} from "./envelope";
import { type ModeInfo, type Usage } from "./commands";

export interface FrameMeta {
  wire: string;
  seq: number;
  ordinal: number;
  replay: boolean;
  ephemeral: boolean;
  /** Session the frame belongs to; absent on connection-level control frames. */
  sessionId?: string;
}

/** One changed configuration key and its real runtime effect. */
export interface ConfigChangedKey {
  key: string;
  scope: string;
}

export interface ToolCallInfo {
  callId: string;
  name: string;
  args: unknown;
}

export interface ApprovalInfo {
  callId: string;
  name?: string;
  sideEffect?: string;
  argsPreview?: unknown;
  requestedAt?: number;
  expiresAt?: number;
  kind?: string;
  tool?: string;
}

export interface BackgroundTaskInfo {
  taskId: string;
  callId?: string | null;
  name?: string;
  kind?: string;
  status?: string;
  adopted?: boolean;
  startedAt?: number;
  startedSeq?: number;
  durationMs?: number;
  finishedAt?: number;
  finishedSeq?: number;
}

export interface SubagentInfo {
  callId: string;
  childSessionId?: string;
  parentSessionId?: string;
  title?: string;
  task?: string;
  tools: string[];
  modelProfile?: string;
  status: "requested" | "running" | "ok" | "failed";
  phase?: string;
  round?: number;
  lastTool?: string;
  messageCount?: number;
  tokensInput?: number;
  tokensOutput?: number;
  summary?: string;
  summaryRef?: string;
  finish?: string;
  code?: string;
  error?: string;
  usage?: Usage;
  rounds?: number;
  durationMs?: number;
  errorInfo?: ErrorInfo | null;
  background?: boolean;
}

export interface SnapshotStreaming {
  requestId?: string;
  kind: "text" | "reasoning";
  text: string;
  truncated: boolean;
}

export interface SnapshotData {
  lastSeq: number;
  phase: string;
  round: number;
  busy: boolean;
  pending: string[];
  streaming: SnapshotStreaming | null;
  approvals: ApprovalInfo[];
  usage: Usage;
  boundModel: string | null;
  mode: ModeInfo | null;
  contextDigest: string | null;
  lastError: ErrorInfo | null;
  background: BackgroundTaskInfo[];
  backgroundRunning: number;
  backgroundTaskIds: string[];
}

export interface ArtifactChunkInfo {
  ref: string;
  offset: number;
  limit: number;
  size: number;
  encoding: string;
  data: string;
  eof: boolean;
  bytes: number;
}

export type ViewFrame =
  | (FrameMeta & {
      kind: "user.message";
      text: string;
      clientMessageId?: string;
      eventId?: string;
      traceId?: string;
    })
  | (FrameMeta & {
      kind: "assistant.started";
      provider?: string;
      model?: string;
      alias?: string;
      round?: number;
      contextDigest?: string;
    })
  | (FrameMeta & { kind: "assistant.delta"; text: string })
  | (FrameMeta & { kind: "reasoning.delta"; text: string })
  | (FrameMeta & {
      kind: "assistant.message";
      text: string;
      reasoning?: string;
      toolCalls: ToolCallInfo[];
      finish?: string;
      usage: Usage;
      cost: Usage;
      provider?: string;
      model?: string;
      round?: number;
      interrupted: boolean;
    })
  | (FrameMeta & { kind: "tool.requested"; callId: string; name: string; args: unknown })
  | (FrameMeta & {
      kind: "tool.started";
      callId: string;
      name?: string;
      sideEffect?: string;
      background: boolean;
    })
  | (FrameMeta & {
      kind: "tool.delta";
      callId: string;
      text: string;
      background: boolean;
      backgroundTaskId?: string;
    })
  | (FrameMeta & {
      kind: "tool.result";
      callId: string;
      name?: string;
      ok: boolean;
      output: unknown;
      outputRef?: string | null;
      outputTokens?: number;
      dedup: boolean;
      code?: string | null;
      error?: string | null;
      errorInfo: ErrorInfo | null;
      background: boolean;
      taskId?: string | null;
      backgroundStatus?: string;
      adopted: boolean;
      startedAt?: number;
    })
  | (FrameMeta & { kind: "approval.required"; approval: ApprovalInfo })
  | (FrameMeta & {
      kind: "approval.resolved";
      callId: string;
      decision: "granted" | "denied";
      reason?: string;
      clientId?: string;
    })
  | (FrameMeta & {
      kind: "agent.completed";
      round?: number;
      finish?: string;
      code?: string;
      errorInfo: ErrorInfo | null;
      recovered: boolean;
    })
  | (FrameMeta & { kind: "notice"; noticeKind: string; payload: Record<string, unknown> })
  | (FrameMeta & {
      kind: "subagent.requested";
      callId: string;
      title?: string;
      task?: string;
      tools: string[];
      modelProfile?: string;
      background: boolean;
    })
  | (FrameMeta & {
      kind: "subagent.started";
      callId: string;
      childSessionId?: string;
      parentSessionId?: string;
      title?: string;
      tools: string[];
      modelProfile?: string;
      readOnly: boolean;
      approvalPolicy?: string;
      timeoutMs?: number;
    })
  | (FrameMeta & {
      kind: "subagent.progress";
      callId: string;
      childSessionId?: string;
      title?: string;
      phase?: string;
      round?: number;
      lastTool?: string;
      messageCount?: number;
      tokensInput?: number;
      tokensOutput?: number;
    })
  | (FrameMeta & {
      kind: "subagent.result";
      subagent: SubagentInfo;
    })
  | (FrameMeta & {
      kind: "subagent.error";
      subagent: SubagentInfo;
    })
  | (FrameMeta & {
      kind: "background.started";
      task: BackgroundTaskInfo;
      startCallId?: string | null;
    })
  | (FrameMeta & {
      kind: "background.terminal";
      status: string;
      task: BackgroundTaskInfo;
      startCallId?: string | null;
    })
  | (FrameMeta & {
      kind: "compaction.started";
      fromIndex?: number;
      toIndex?: number;
      targetTokens?: number;
      sourceDigest?: string;
    })
  | (FrameMeta & {
      kind: "compaction.applied";
      fromIndex?: number;
      toIndex?: number;
      summary?: string;
      summaryRef?: string;
      tokens?: number;
      summarizer?: string;
      fallback: boolean;
      errorInfo: ErrorInfo | null;
    })
  | (FrameMeta & { kind: "mode"; mode: ModeInfo })
  | (FrameMeta & { kind: "phase.changed"; from: string; to: string })
  | (FrameMeta & { kind: "error"; errorInfo: ErrorInfo; code?: string | null; round?: number })
  | (FrameMeta & { kind: "snapshot"; snapshot: SnapshotData })
  | (FrameMeta & { kind: "gap"; fromSeq: number; toSeq: number })
  | (FrameMeta & { kind: "flow"; droppedEphemeral: number; throttled: boolean })
  | (FrameMeta & { kind: "bye"; reason?: string })
  | (FrameMeta & {
      kind: "config.changed";
      revision: string;
      keys: ConfigChangedKey[];
    })
  | (FrameMeta & { kind: "artifact.chunk"; chunk: ArtifactChunkInfo });

function meta(frame: FrameEnvelope, wire: string): FrameMeta {
  return {
    wire,
    seq: frame.seq ?? 0,
    ordinal: frame.ordinal,
    replay: frame.replay,
    ephemeral: frame.ephemeral,
    sessionId: frame.session_id,
  };
}

export function messageText(message: Record<string, unknown>): string {
  const content = message.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === "string") return part;
        if (isRecord(part) && part.type === "text" && typeof part.text === "string") {
          return part.text;
        }
        return "";
      })
      .join("");
  }
  return typeof message.text === "string" ? message.text : "";
}

export function messageReasoning(message: Record<string, unknown>): string {
  return typeof message.reasoning === "string" ? message.reasoning : "";
}

function parseToolCalls(value: unknown): ToolCallInfo[] {
  return asArray(value)
    .map((entry) => {
      const record = asRecord(entry);
      return {
        callId: asString(record.call_id) ?? "",
        name: asString(record.name) ?? "",
        args: record.args,
      };
    })
    .filter((call) => call.callId !== "");
}

function parseApproval(payload: Record<string, unknown>): ApprovalInfo {
  return {
    callId: asString(payload.call_id) ?? "",
    name: asString(payload.name),
    sideEffect: asString(payload.side_effect),
    argsPreview: payload.args_preview,
    requestedAt: asNumber(payload.requested_at),
    expiresAt: asNumber(payload.expires_at),
    kind: asString(payload.kind),
    tool: asString(payload.tool),
  };
}

export function parseBackgroundTask(payload: Record<string, unknown>): BackgroundTaskInfo {
  return {
    taskId: asString(payload.task_id) ?? "",
    callId: asString(payload.call_id) ?? null,
    name: asString(payload.name),
    kind: asString(payload.kind),
    status: asString(payload.status),
    adopted: asBoolean(payload.adopted),
    startedAt: asNumber(payload.started_at),
    startedSeq: asNumber(payload.started_seq),
    durationMs: asNumber(payload.duration_ms),
    finishedAt: asNumber(payload.finished_at),
    finishedSeq: asNumber(payload.finished_seq),
  };
}

function parseMode(value: unknown): ModeInfo | null {
  if (!isRecord(value)) return null;
  const name = asString(value.name);
  if (!name) return null;
  const source = asString(value.source);
  // A freshly activated session seeds the snapshot from the host summary,
  // which may predate any `system.mode.changed` event and omit the id (older
  // gateways always do). Derive the canonical `<source>/<name>` id — the same
  // fallback as the TUI — instead of discarding the mode.
  const id = asString(value.id) ?? (source ? `${source}/${name}` : name);
  return {
    id,
    name,
    source,
    digest: asString(value.digest) ?? null,
    from: asString(value.from) ?? null,
    refresh: asBoolean(value.refresh),
  };
}

function parseSnapshot(payload: Record<string, unknown>): SnapshotData {
  const streaming = isRecord(payload.streaming)
    ? {
        requestId: asString(payload.streaming.request_id),
        kind: payload.streaming.kind === "reasoning" ? ("reasoning" as const) : ("text" as const),
        text: asString(payload.streaming.partial_text) ?? "",
        truncated: asBoolean(payload.streaming.truncated),
      }
    : null;

  return {
    lastSeq: asNumber(payload.last_seq) ?? 0,
    phase: asString(payload.phase) ?? "idle",
    round: asNumber(payload.round) ?? 0,
    busy: asBoolean(payload.busy),
    pending: asStringArray(payload.pending),
    streaming,
    approvals: asArray(payload.pending_approvals)
      .map((entry) => parseApproval(asRecord(entry)))
      .filter((approval) => approval.callId !== ""),
    usage: asRecord(payload.usage) as Usage,
    boundModel: asString(payload.bound_model) ?? null,
    mode: parseMode(payload.mode),
    contextDigest: asString(payload.context_digest) ?? null,
    lastError: asErrorInfo(payload.last_error),
    background: asArray(payload.background)
      .map((entry) => parseBackgroundTask(asRecord(entry)))
      .filter((task) => task.taskId !== ""),
    backgroundRunning: asNumber(payload.background_running) ?? 0,
    backgroundTaskIds: asStringArray(payload.background_task_ids),
  };
}

function parseArtifactChunk(payload: Record<string, unknown>): ArtifactChunkInfo {
  const data = asString(payload.data) ?? "";
  return {
    ref: asString(payload.ref) ?? "",
    offset: asNumber(payload.offset) ?? 0,
    limit: asNumber(payload.limit) ?? 0,
    size: asNumber(payload.size) ?? 0,
    encoding: asString(payload.encoding) ?? "base64",
    data,
    eof: asBoolean(payload.eof),
    bytes: base64Bytes(data),
  };
}

export function base64Bytes(data: string): number {
  if (!data) return 0;
  const padding = data.endsWith("==") ? 2 : data.endsWith("=") ? 1 : 0;
  return Math.max(0, Math.floor((data.length * 3) / 4) - padding);
}

/** Parse one wire frame. Returns `null` for unknown frames. */
export function parseFrame(frame: FrameEnvelope): ViewFrame | null {
  const payload = frame.payload;
  const m = meta(frame, frame.frame);

  switch (frame.frame) {
    case "user.message":
      return {
        ...m,
        kind: "user.message",
        text: asString(payload.text) ?? "",
        clientMessageId: asString(payload.client_message_id),
        eventId: asString(payload.event_id),
        traceId: asString(payload.trace_id),
      };

    case "assistant.started":
      return {
        ...m,
        kind: "assistant.started",
        provider: asString(payload.provider),
        model: asString(payload.model),
        alias: asString(payload.alias),
        round: asNumber(payload.round),
        contextDigest: asString(payload.context_digest),
      };

    case "assistant.delta":
      return { ...m, kind: "assistant.delta", text: asString(payload.text) ?? "" };

    case "reasoning.delta":
      return { ...m, kind: "reasoning.delta", text: asString(payload.text) ?? "" };

    case "assistant.message": {
      const message = asRecord(payload.message);
      return {
        ...m,
        kind: "assistant.message",
        text: messageText(message),
        reasoning: messageReasoning(message),
        toolCalls: parseToolCalls(payload.tool_calls),
        finish: asString(payload.finish),
        usage: asRecord(payload.usage) as Usage,
        cost: asRecord(payload.cost) as Usage,
        provider: asString(payload.provider),
        model: asString(payload.model_actual),
        round: asNumber(payload.round),
        interrupted: asBoolean(payload.interrupted),
      };
    }

    case "tool.requested":
      return {
        ...m,
        kind: "tool.requested",
        callId: asString(payload.call_id) ?? "",
        name: asString(payload.name) ?? "",
        args: payload.args,
      };

    case "tool.started":
      return {
        ...m,
        kind: "tool.started",
        callId: asString(payload.call_id) ?? "",
        name: asString(payload.name),
        sideEffect: asString(payload.side_effect),
        background: asBoolean(payload.background),
      };

    case "tool.delta":
      return {
        ...m,
        kind: "tool.delta",
        callId: asString(payload.call_id) ?? "",
        text: asString(payload.text) ?? "",
        background: asBoolean(payload.background),
        backgroundTaskId: asString(payload.background_task_id),
      };

    case "tool.result":
      return {
        ...m,
        kind: "tool.result",
        callId: asString(payload.call_id) ?? "",
        name: asString(payload.name),
        ok: asBoolean(payload.ok),
        output: payload.output,
        outputRef: asString(payload.output_ref) ?? null,
        outputTokens: asNumber(payload.output_tokens),
        dedup: asBoolean(payload.dedup),
        code: asString(payload.code) ?? null,
        error: asString(payload.error) ?? null,
        errorInfo: asErrorInfo(payload.error_info),
        background: asBoolean(payload.background),
        taskId: asString(payload.task_id) ?? null,
        backgroundStatus: asString(payload.background_status),
        adopted: asBoolean(payload.adopted),
        startedAt: asNumber(payload.started_at),
      };

    case "approval.required":
      return { ...m, kind: "approval.required", approval: parseApproval(payload) };

    case "approval.resolved":
      return {
        ...m,
        kind: "approval.resolved",
        callId: asString(payload.call_id) ?? "",
        decision: payload.decision === "denied" ? "denied" : "granted",
        reason: asString(payload.reason),
        clientId: asString(payload.client_id),
      };

    case "agent.completed":
      return {
        ...m,
        kind: "agent.completed",
        round: asNumber(payload.round),
        finish: asString(payload.finish),
        code: asString(payload.code),
        errorInfo: asErrorInfo(payload.error_info),
        recovered: asBoolean(payload.recovered),
      };

    case "notice":
      return {
        ...m,
        kind: "notice",
        noticeKind: asString(payload.kind) ?? "notice",
        payload,
      };

    case "subagent.requested":
      return {
        ...m,
        kind: "subagent.requested",
        callId: asString(payload.call_id) ?? "",
        title: asString(payload.title),
        task: asString(payload.task),
        tools: asStringArray(payload.tools),
        modelProfile: asString(payload.model_profile),
        background: asBoolean(payload.background),
      };

    case "subagent.started":
      return {
        ...m,
        kind: "subagent.started",
        callId: asString(payload.call_id) ?? "",
        childSessionId: asString(payload.child_session_id),
        parentSessionId: asString(payload.parent_session_id),
        title: asString(payload.title),
        tools: asStringArray(payload.tools),
        modelProfile: asString(payload.model_profile),
        readOnly: asBoolean(payload.read_only),
        approvalPolicy: asString(payload.approval_policy),
        timeoutMs: asNumber(payload.timeout_ms),
      };

    case "subagent.progress":
      return {
        ...m,
        kind: "subagent.progress",
        callId: asString(payload.call_id) ?? "",
        childSessionId: asString(payload.child_session_id),
        title: asString(payload.title),
        phase: asString(payload.phase),
        round: asNumber(payload.round),
        lastTool: asString(payload.last_tool),
        messageCount: asNumber(payload.message_count),
        tokensInput: asNumber(payload.tokens_input),
        tokensOutput: asNumber(payload.tokens_output),
      };

    case "subagent.result": {
      const subagent: SubagentInfo = {
        callId: asString(payload.call_id) ?? "",
        childSessionId: asString(payload.child_session_id),
        title: asString(payload.title),
        tools: [],
        status: "ok",
        finish: asString(payload.finish),
        summary: asString(payload.summary),
        summaryRef: asString(payload.summary_ref),
        usage: asRecord(payload.usage) as Usage,
        rounds: asNumber(payload.rounds),
        durationMs: asNumber(payload.duration_ms),
        modelProfile: asString(payload.model_profile),
      };
      return { ...m, kind: "subagent.result", subagent };
    }

    case "subagent.error": {
      const subagent: SubagentInfo = {
        callId: asString(payload.call_id) ?? "",
        childSessionId: asString(payload.child_session_id),
        title: asString(payload.title),
        tools: [],
        status: "failed",
        code: asString(payload.code),
        errorInfo: asErrorInfo(payload.error_info),
        usage: asRecord(payload.usage) as Usage,
        rounds: asNumber(payload.rounds),
        durationMs: asNumber(payload.duration_ms),
      };
      return { ...m, kind: "subagent.error", subagent };
    }

    case "background.started":
      return {
        ...m,
        kind: "background.started",
        task: parseBackgroundTask(payload),
        startCallId: asString(payload.start_call_id) ?? null,
      };

    case "background.result":
    case "background.failed":
    case "background.stopped":
      return {
        ...m,
        kind: "background.terminal",
        status:
          asString(payload.status) ??
          (frame.frame === "background.result"
            ? "completed"
            : frame.frame === "background.stopped"
              ? "stopped"
              : "failed"),
        task: parseBackgroundTask(payload),
        startCallId: asString(payload.start_call_id) ?? null,
      };

    case "compaction.started":
      return {
        ...m,
        kind: "compaction.started",
        fromIndex: asNumber(payload.from_index),
        toIndex: asNumber(payload.to_index),
        targetTokens: asNumber(payload.target_tokens),
        sourceDigest: asString(payload.source_digest),
      };

    case "compaction.applied":
      return {
        ...m,
        kind: "compaction.applied",
        fromIndex: asNumber(payload.from_index),
        toIndex: asNumber(payload.to_index),
        summary: asString(payload.summary),
        summaryRef: asString(payload.summary_ref),
        tokens: asNumber(payload.tokens),
        summarizer: asString(payload.summarizer),
        fallback: asBoolean(payload.fallback),
        errorInfo: asErrorInfo(payload.error_info),
      };

    case "mode": {
      const mode = parseMode(payload);
      return mode ? { ...m, kind: "mode", mode } : null;
    }

    case "phase.changed":
      return {
        ...m,
        kind: "phase.changed",
        from: asString(payload.from) ?? "",
        to: asString(payload.to) ?? "",
      };

    case "error":
      return {
        ...m,
        kind: "error",
        errorInfo:
          asErrorInfo(payload.error_info) ?? {
            code: asString(payload.code) ?? "E_UNKNOWN",
            message: asString(payload.error) ?? "unknown error",
            source: "gateway",
          },
        code: asString(payload.code) ?? null,
        round: asNumber(payload.round),
      };

    case "snapshot":
      return {
        ...m,
        kind: "snapshot",
        snapshot: parseSnapshot(payload),
        sessionId: frame.session_id,
      };

    case "gap":
      return {
        ...m,
        kind: "gap",
        fromSeq: asNumber(payload.from_seq) ?? 0,
        toSeq: asNumber(payload.to_seq) ?? 0,
      };

    case "flow":
      return {
        ...m,
        kind: "flow",
        droppedEphemeral: asNumber(payload.dropped_ephemeral) ?? 0,
        throttled: asBoolean(payload.throttled),
      };

    case "bye":
      return { ...m, kind: "bye", reason: asString(payload.reason) };

    case "config.changed": {
      const revision = asString(payload.revision);
      if (!revision) return null;
      const keys = asArray(payload.keys)
        .map((item) => {
          const record = asRecord(item);
          const key = asString(record?.key);
          if (!key) return null;
          return { key, scope: asString(record?.scope) || "new_session" };
        })
        .filter((item): item is ConfigChangedKey => item !== null);
      return { ...m, kind: "config.changed", revision, keys };
    }

    case "artifact.chunk":
      return { ...m, kind: "artifact.chunk", chunk: parseArtifactChunk(payload) };

    default:
      return null;
  }
}
