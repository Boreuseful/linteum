/**
 * Gateway Protocol v1 envelopes.
 *
 * Mirrors `linteum_agent/gateway/protocol.py` without importing Python or any
 * runtime code. Every raw message is normalized here; components never touch
 * wire JSON.
 */

export const PROTOCOL_VERSION = 1;

export interface ErrorInfo {
  code: string;
  source?: string;
  message: string;
  detail?: string;
  retryable?: boolean;
  enters_history?: boolean;
  context?: Record<string, unknown>;
}

export interface CommandEnvelope {
  v: number;
  kind: "cmd";
  id: string;
  name: string;
  args: Record<string, unknown>;
  session_id?: string;
  trace_id?: string;
}

export interface ResultEnvelope {
  v: number;
  kind: "result";
  id: string;
  reply_to: string;
  ok: boolean;
  result: Record<string, unknown>;
  error?: ErrorInfo;
  name?: string;
}

export interface FrameEnvelope {
  v: number;
  kind: "frame";
  id: string;
  frame: string;
  payload: Record<string, unknown>;
  session_id?: string;
  seq?: number;
  ephemeral: boolean;
  replay: boolean;
  ordinal: number;
}

export type Envelope = ResultEnvelope | FrameEnvelope;

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function asRecord(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

export function asString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

export function asNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

export function asBoolean(value: unknown): boolean {
  return value === true;
}

export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function asStringArray(value: unknown): string[] {
  return asArray(value).filter((item): item is string => typeof item === "string");
}

export function asErrorInfo(value: unknown): ErrorInfo | null {
  if (!isRecord(value)) return null;
  const code = asString(value.code);
  if (!code) return null;
  return {
    code,
    source: asString(value.source),
    message: asString(value.message) ?? code,
    detail: asString(value.detail),
    retryable: value.retryable === true,
    enters_history: value.enters_history === true,
    context: isRecord(value.context) ? value.context : undefined,
  };
}

export function isResultEnvelope(value: unknown): value is ResultEnvelope {
  return (
    isRecord(value) &&
    value.kind === "result" &&
    typeof value.reply_to === "string" &&
    typeof value.ok === "boolean"
  );
}

export function isFrameEnvelope(value: unknown): value is FrameEnvelope {
  return isRecord(value) && value.kind === "frame" && typeof value.frame === "string";
}

export function decodeEnvelope(raw: string): Envelope | null {
  try {
    const value: unknown = JSON.parse(raw);
    if (isResultEnvelope(value)) {
      return {
        v: asNumber(value.v) ?? PROTOCOL_VERSION,
        kind: "result",
        id: asString(value.id) ?? "",
        reply_to: value.reply_to,
        ok: value.ok,
        result: asRecord(value.result),
        error: asErrorInfo(value.error) ?? undefined,
        name: asString(value.name),
      };
    }
    if (isFrameEnvelope(value)) {
      return {
        v: asNumber(value.v) ?? PROTOCOL_VERSION,
        kind: "frame",
        id: asString(value.id) ?? "",
        frame: value.frame,
        payload: asRecord(value.payload),
        session_id: asString(value.session_id),
        seq: asNumber(value.seq),
        ephemeral: asBoolean(value.ephemeral),
        replay: asBoolean(value.replay),
        ordinal: asNumber(value.ordinal) ?? 0,
      };
    }
    return null;
  } catch {
    return null;
  }
}

let envelopeCounter = 0;

export function newEnvelopeId(prefix: string): string {
  envelopeCounter += 1;
  const random =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID().slice(0, 8)
      : Math.random().toString(16).slice(2, 10);
  return `${prefix}-${Date.now().toString(36)}-${envelopeCounter.toString(36)}-${random}`;
}

export function encodeCommand(command: CommandEnvelope): string {
  return JSON.stringify(command);
}

export function makeCommand(
  name: string,
  args: Record<string, unknown> = {},
  sessionId?: string,
  traceId?: string,
): CommandEnvelope {
  const command: CommandEnvelope = {
    v: PROTOCOL_VERSION,
    kind: "cmd",
    id: newEnvelopeId("c"),
    name,
    args,
  };
  if (sessionId) command.session_id = sessionId;
  if (traceId) command.trace_id = traceId;
  return command;
}

/** Control frames bypass the subscription filter and are never deduplicated. */
export const CONTROL_FRAME_NAMES = new Set([
  "snapshot",
  "gap",
  "flow",
  "error",
  "pong",
  "bye",
  "history.page",
  "artifact.chunk",
]);
