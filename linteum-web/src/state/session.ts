import type { SessionInfo, Usage } from "../protocol/commands";
import type { SnapshotData } from "../protocol/frames";
import type { ActiveSessionState } from "./types";

export function initialActiveSession(
  id: string,
  info: SessionInfo | null,
): ActiveSessionState {
  return {
    id,
    info,
    phase: "idle",
    round: 0,
    busy: false,
    pending: [],
    streaming: null,
    usage: {},
    boundModel: null,
    mode: null,
    contextDigest: null,
    lastError: null,
    lastSeq: 0,
    // Every new active session waits for its `session.subscribe` replay.
    hydrating: true,
    seen: new Set(),
    transcript: [],
    approvals: [],
    background: {},
    subagents: {},
    childSessions: [],
    modes: [],
    modesLoading: false,
    context: null,
    contextLoading: false,
    contextError: null,
    tasksLoading: false,
  };
}

export function mergeUsage(base: Usage, delta: Usage): Usage {
  const result: Usage = { ...base };
  for (const [key, value] of Object.entries(delta)) {
    if (typeof value === "number" && Number.isFinite(value)) {
      const current = typeof result[key] === "number" ? (result[key] as number) : 0;
      result[key] = current + value;
    } else if (!(key in result)) {
      result[key] = value;
    }
  }
  return result;
}

export function applySnapshot(
  active: ActiveSessionState,
  snapshot: SnapshotData,
): ActiveSessionState {
  const background: ActiveSessionState["background"] = {};
  for (const task of snapshot.background) {
    background[task.taskId] = task;
  }
  // A snapshot after a reconnect can report a binding that changed while the
  // socket was down; the cached context plan still embeds the old tool table.
  const modeChanged =
    active.mode?.id !== snapshot.mode?.id ||
    (active.mode?.digest ?? null) !== (snapshot.mode?.digest ?? null);
  return {
    ...active,
    phase: snapshot.phase,
    round: snapshot.round,
    busy: snapshot.busy,
    pending: [...snapshot.pending],
    streaming: snapshot.streaming
      ? {
          requestId: snapshot.streaming.requestId,
          kind: snapshot.streaming.kind,
          text: snapshot.streaming.text,
        }
      : null,
    usage: snapshot.usage ?? {},
    boundModel: snapshot.boundModel,
    mode: snapshot.mode,
    contextDigest: snapshot.contextDigest,
    lastError: snapshot.lastError,
    approvals: snapshot.approvals,
    background,
    lastSeq: Math.max(active.lastSeq, snapshot.lastSeq),
    ...(modeChanged
      ? { context: null, contextLoading: false, contextError: null }
      : {}),
  };
}

export function resetForNewInstance(active: ActiveSessionState): ActiveSessionState {
  return initialActiveSession(active.id, active.info);
}

export function phaseIsBusy(phase: string): boolean {
  return phase !== "idle" && phase !== "paused";
}
