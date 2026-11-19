/**
 * Root reducer: pure state transitions + effects.
 *
 * Effects are executed by transport/controller.ts. The reducer never touches
 * the network, localStorage or React.
 */

import {
  ARTIFACT_CHUNK_BYTES,
  MAX_ARTIFACT_BYTES,
  MAX_FILE_PANE_WIDTH,
  MAX_LEFT_RAIL_WIDTH,
  MAX_RIGHT_RAIL_WIDTH,
  MAX_SEEN_FRAMES,
  MAX_TRANSCRIPT_ITEMS,
  MIN_FILE_PANE_WIDTH,
  MIN_LEFT_RAIL_WIDTH,
  MIN_RIGHT_RAIL_WIDTH,
  STORAGE_KEYS,
} from "../config";
import {
  approvalDecideArgs,
  artifactReadArgs,
  childrenArgs,
  configFallbackDeleteArgs,
  configFallbackMoveArgs,
  configFallbackSetArgs,
  configGetArgs,
  configProfileDeleteArgs,
  configProfileDuplicateArgs,
  configProfileSetArgs,
  configSetArgs,
  configUnsetArgs,
  modeSetArgs,
  pluginSetAllowedArgs,
  sessionCreateArgs,
  sessionListArgs,
  sessionSubscribeArgs,
  taskStopArgs,
  type ConfigIssue,
  type ConfigSchema,
  type PluginSummary,
} from "../protocol/commands";
import { CONTROL_FRAME_NAMES } from "../protocol/envelope";
import { base64Bytes, type BackgroundTaskInfo, type SubagentInfo, type ViewFrame } from "../protocol/frames";
import { makeActivity, pushActivity } from "./activity";
import { removeApproval, upsertApproval } from "./approvals";
import { mergeTasks, upsertTask } from "./background";
import { INITIAL_CONFIGURATION, configKeySection } from "./configuration";
import { withAllowed } from "./plugins";
import {
  applySnapshot,
  initialActiveSession,
  mergeUsage,
  phaseIsBusy,
  resetForNewInstance,
} from "./session";
import { sortSessions, upsertSession } from "./sessions";
import { toggleOpenPanel } from "./sidebar";
import { mergeSubagent } from "./subagents";
import {
  appendAssistantDelta,
  appendError,
  appendNotice,
  appendUser,
  finalizeAssistant,
  startAssistant,
  upsertCompaction,
  upsertSubagentItem,
  upsertTool,
} from "./transcript";
import type {
  Action,
  ActiveSessionState,
  AppState,
  Effect,
  NoticeItem,
  OverlayState,
  TranscriptItem,
  UiState,
} from "./types";

interface Reduced {
  state: AppState;
  effects: Effect[];
}

function noEffects(state: AppState): Reduced {
  return { state, effects: [] };
}

/** Shared precondition for every configuration write (web-config §8.3). */
function configSaveAllowed(state: AppState): boolean {
  const configuration = state.configuration;
  return (
    state.connection.status === "ready" &&
    configuration.writable &&
    (configuration.revision !== "" || !configuration.exists)
  );
}

/** Marks a configuration write in flight and clears the previous outcome. */
function savingConfiguration(state: AppState): AppState {
  return {
    ...state,
    configuration: {
      ...state.configuration,
      saving: true,
      error: null,
      lastSave: null,
      conflict: null,
    },
  };
}

function mergeConfigSchema(
  current: ConfigSchema | null,
  incoming: ConfigSchema | null,
): ConfigSchema | null {
  if (!incoming) return current;
  if (!current) return incoming;
  const sections = new Map(current.sections.map((section) => [section.key, section]));
  for (const section of incoming.sections) sections.set(section.key, section);
  return { ...incoming, sections: [...sections.values()] };
}

function configIssuesInvalid(issues: ConfigIssue[]): boolean {
  return issues.some((issue) => issue.severity === "error");
}

function draftsDirty(drafts: Record<string, boolean>): boolean {
  return Object.values(drafts).some(Boolean);
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/** The file preview belongs to one session's workspace. */
function clearedFilePane(ui: UiState): UiState {
  return {
    ...ui,
    openFiles: [],
    activeFilePath: null,
    filePaneOpen: false,
  };
}

function removeOverlay(overlays: OverlayState[], kind: OverlayState["kind"]): OverlayState[] {
  const index = overlays.findIndex((overlay) => overlay.kind === kind);
  if (index < 0) return overlays;
  const copy = overlays.slice();
  copy.splice(index, 1);
  return copy;
}

function frameKey(frame: ViewFrame): string | null {
  if (frame.ephemeral || CONTROL_FRAME_NAMES.has(frame.wire) || frame.seq <= 0) {
    return null;
  }
  const identity =
    "callId" in frame
      ? frame.callId
      : "clientMessageId" in frame
        ? (frame.clientMessageId ?? "")
        : "";
  return `${frame.seq}:${frame.wire}:${frame.ordinal}:${identity}`;
}

function markSeen(active: ActiveSessionState, key: string | null): ActiveSessionState {
  if (!key) return active;
  const seen = new Set(active.seen);
  seen.add(key);
  if (seen.size > MAX_SEEN_FRAMES) {
    const excess = seen.size - MAX_SEEN_FRAMES;
    let removed = 0;
    for (const value of seen) {
      seen.delete(value);
      removed += 1;
      if (removed >= excess) break;
    }
  }
  return { ...active, seen };
}

function appendGapNotice(items: TranscriptItem[], frame: Extract<ViewFrame, { kind: "gap" }>): TranscriptItem[] {
  const item: NoticeItem = {
    kind: "notice",
    id: `notice-gap-${frame.seq}-${frame.fromSeq}-${frame.toSeq}`,
    seq: frame.seq,
    ordinal: frame.ordinal,
    createdAt: 0,
    noticeKind: "gap",
    payload: { from_seq: frame.fromSeq, to_seq: frame.toSeq },
  };
  const next = [...items, item];
  return next.length > MAX_TRANSCRIPT_ITEMS ? next.slice(-MAX_TRANSCRIPT_ITEMS) : next;
}

function applySubagentUpdate(
  active: ActiveSessionState,
  callId: string,
  update: Partial<SubagentInfo> & { callId: string },
): ActiveSessionState {
  if (!callId) return active;
  return {
    ...active,
    subagents: {
      ...active.subagents,
      [callId]: mergeSubagent(active.subagents[callId], update),
    },
    transcript: upsertSubagentItem(active.transcript, callId, (current) =>
      mergeSubagent(current, update),
    ),
  };
}

function reduceFrame(state: AppState, frame: ViewFrame): Reduced {
  let ui = state.ui;

  // Session isolation: a frame for another (or a closed) session must never
  // touch the active transcript. Unsubscribing is asynchronous, so in-flight
  // frames from the previous session are expected during a switch.
  if (frame.sessionId && (!state.active || state.active.id !== frame.sessionId)) {
    return noEffects(state);
  }

  if (frame.kind === "snapshot") {
    const sessionId = frame.sessionId ?? state.active?.id ?? "";
    if (!sessionId) return noEffects(state);
    const base =
      state.active && state.active.id === sessionId
        ? state.active
        : initialActiveSession(sessionId, null);
    return {
      state: { ...state, active: applySnapshot(base, frame.snapshot) },
      effects: [],
    };
  }

  if (frame.kind === "gap") {
    if (!state.active) return noEffects(state);
    ui = {
      ...ui,
      activity: pushActivity(
        ui.activity,
        makeActivity("gap", `History gap ${frame.fromSeq}..${frame.toSeq}; resubscribing.`),
      ),
    };
    const active: ActiveSessionState = {
      ...state.active,
      lastSeq: Math.max(state.active.lastSeq, frame.toSeq),
      transcript: appendGapNotice(state.active.transcript, frame),
    };
    return {
      state: { ...state, active, ui },
      effects: [
        {
          type: "command",
          name: "session.subscribe",
          args: sessionSubscribeArgs(frame.toSeq),
          sessionId: active.id,
        },
      ],
    };
  }

  if (frame.kind === "flow") {
    ui = {
      ...ui,
      activity: pushActivity(
        ui.activity,
        makeActivity(
          "flow",
          `Throttled: ${frame.droppedEphemeral} ephemeral update(s) dropped.`,
        ),
      ),
    };
    return { state: { ...state, ui }, effects: [] };
  }

  if (frame.kind === "bye") {
    ui = {
      ...ui,
      activity: pushActivity(
        ui.activity,
        makeActivity("connection", `Session closed: ${frame.reason ?? "bye"}`),
      ),
    };
    return {
      state: { ...state, active: null, ui },
      effects: [{ type: "command", name: "session.list", args: sessionListArgs({}) }],
    };
  }

  if (frame.kind === "artifact.chunk") {
    const chunk = frame.chunk;
    if (!chunk.ref) return noEffects(state);
    const current = ui.artifacts[chunk.ref] ?? {
      ref: chunk.ref,
      chunks: [],
      size: chunk.size,
      eof: false,
      loading: true,
      error: null,
    };
    const chunks = [...current.chunks, chunk.data];
    const bytes = chunks.reduce((total, part) => total + base64Bytes(part), 0);
    const finished = chunk.eof || bytes >= MAX_ARTIFACT_BYTES;
    const artifact = {
      ...current,
      chunks,
      size: chunk.size,
      eof: chunk.eof,
      loading: !finished,
    };
    const effects: Effect[] = [];
    if (!finished) {
      effects.push({
        type: "command",
        name: "artifact.read",
        args: artifactReadArgs(chunk.ref, chunk.offset + chunk.bytes, ARTIFACT_CHUNK_BYTES),
        sessionId: state.active?.id,
      });
    }
    return {
      state: {
        ...state,
        ui: { ...ui, artifacts: { ...ui.artifacts, [chunk.ref]: artifact } },
      },
      effects,
    };
  }

  const active = state.active;
  if (!active) return noEffects(state);

  const key = frameKey(frame);
  if (key && active.seen.has(key)) return noEffects(state);

  let next = markSeen(active, key);
  const effects: Effect[] = [];
  let catalog = state.catalog;

  switch (frame.kind) {
    case "user.message":
      next = { ...next, transcript: appendUser(next.transcript, frame) };
      break;

    case "assistant.started":
      next = { ...next, transcript: startAssistant(next.transcript, frame) };
      break;

    case "assistant.delta":
      next = {
        ...next,
        transcript: appendAssistantDelta(next.transcript, "text", frame.text),
      };
      break;

    case "reasoning.delta":
      next = {
        ...next,
        transcript: appendAssistantDelta(next.transcript, "reasoning", frame.text),
      };
      break;

    case "assistant.message": {
      const usage = frame.replay ? next.usage : mergeUsage(next.usage, frame.usage);
      next = {
        ...next,
        transcript: finalizeAssistant(next.transcript, frame),
        usage,
        boundModel:
          frame.provider && frame.model
            ? `${frame.provider}/${frame.model}`
            : next.boundModel,
        streaming: null,
      };
      break;
    }

    case "tool.requested":
    case "tool.started":
    case "tool.delta":
    case "tool.result":
      next = { ...next, transcript: upsertTool(next.transcript, frame) };
      if (frame.kind === "tool.result") {
        // The gateway drops the approval on the terminal; mirror it so a
        // replayed request cannot leave a stale blocking prompt behind.
        next = { ...next, approvals: removeApproval(next.approvals, frame.callId) };
        if (!frame.ok && !frame.background) {
          next = { ...next, lastError: frame.errorInfo };
        }
      }
      break;

    case "approval.required":
      // A replay starts with an authoritative snapshot of pending approvals.
      // Replaying the historical lifecycle would resurrect approvals that the
      // projector cleared without emitting `approval.resolved` (interrupts,
      // tool terminals). Live frames (replay=false) still update the set.
      if (!frame.replay) {
        next = { ...next, approvals: upsertApproval(next.approvals, frame.approval) };
      }
      break;

    case "approval.resolved":
      if (!frame.replay) {
        next = { ...next, approvals: removeApproval(next.approvals, frame.callId) };
      }
      break;

    case "agent.completed":
      next = { ...next, round: frame.round ?? next.round };
      if (frame.finish === "error" && frame.errorInfo) {
        next = { ...next, lastError: frame.errorInfo };
      }
      break;

    case "notice":
      next = { ...next, transcript: appendNotice(next.transcript, frame) };
      if (frame.noticeKind === "interrupt") {
        // Background cancels are task-local; foreground interrupts resolve
        // either every approval (scope "all") or the named one.
        const scope =
          typeof frame.payload.scope === "string" ? frame.payload.scope : "foreground";
        const callId =
          typeof frame.payload.call_id === "string" ? frame.payload.call_id : null;
        const approvals =
          scope === "background"
            ? next.approvals
            : scope === "all" || !callId
              ? []
              : removeApproval(next.approvals, callId);
        next = { ...next, busy: false, streaming: null, approvals };
      }
      break;

    case "subagent.requested":
      next = applySubagentUpdate(next, frame.callId, {
        callId: frame.callId,
        title: frame.title,
        task: frame.task,
        tools: frame.tools,
        modelProfile: frame.modelProfile,
        status: "requested",
        background: frame.background,
      });
      break;

    case "subagent.started":
      next = applySubagentUpdate(next, frame.callId, {
        callId: frame.callId,
        childSessionId: frame.childSessionId,
        parentSessionId: frame.parentSessionId,
        title: frame.title,
        tools: frame.tools,
        modelProfile: frame.modelProfile,
        status: "running",
        phase: "thinking",
      });
      break;

    case "subagent.progress":
      next = applySubagentUpdate(next, frame.callId, {
        callId: frame.callId,
        childSessionId: frame.childSessionId,
        title: frame.title,
        phase: frame.phase,
        round: frame.round,
        lastTool: frame.lastTool,
        messageCount: frame.messageCount,
        tokensInput: frame.tokensInput,
        tokensOutput: frame.tokensOutput,
        status: "running",
      });
      break;

    case "subagent.result":
      next = applySubagentUpdate(next, frame.subagent.callId, {
        callId: frame.subagent.callId,
        childSessionId: frame.subagent.childSessionId,
        title: frame.subagent.title,
        status: "ok",
        finish: frame.subagent.finish,
        summary: frame.subagent.summary,
        summaryRef: frame.subagent.summaryRef,
        usage: frame.subagent.usage,
        rounds: frame.subagent.rounds,
        durationMs: frame.subagent.durationMs,
      });
      break;

    case "subagent.error":
      next = applySubagentUpdate(next, frame.subagent.callId, {
        callId: frame.subagent.callId,
        childSessionId: frame.subagent.childSessionId,
        title: frame.subagent.title,
        status: "failed",
        code: frame.subagent.code,
        errorInfo: frame.subagent.errorInfo,
        usage: frame.subagent.usage,
        rounds: frame.subagent.rounds,
        durationMs: frame.subagent.durationMs,
      });
      break;

    case "background.started":
      next = { ...next, background: upsertTask(next.background, frame.task) };
      break;

    case "background.terminal": {
      const task: BackgroundTaskInfo = { ...frame.task, status: frame.status };
      next = { ...next, background: upsertTask(next.background, task) };
      ui = {
        ...ui,
        activity: pushActivity(
          ui.activity,
          makeActivity("notice", `Background task ${frame.task.taskId} ${frame.status}.`),
        ),
      };
      break;
    }

    case "compaction.started":
    case "compaction.applied":
      next = { ...next, transcript: upsertCompaction(next.transcript, frame) };
      break;

    case "mode": {
      const previous = next.mode;
      const bindingChanged =
        previous === null ||
        previous.id !== frame.mode.id ||
        (previous.digest ?? null) !== (frame.mode.digest ?? null);
      next = { ...next, mode: frame.mode };
      if (bindingChanged) {
        // The derived context plan embeds the mode's tool table, and the
        // context panel caches the last plan. Drop the cache so an open panel
        // refetches against the new binding instead of showing stale tools.
        next = {
          ...next,
          context: null,
          contextLoading: false,
          contextError: null,
        };
      }
      break;
    }

    case "phase.changed":
      next = {
        ...next,
        phase: frame.to,
        busy: phaseIsBusy(frame.to),
        streaming: frame.to === "idle" ? null : next.streaming,
      };
      catalog = {
        ...catalog,
        items: catalog.items.map((item) =>
          item.session_id === next.id ? { ...item, phase: frame.to } : item,
        ),
      };
      break;

    case "error":
      next = {
        ...next,
        transcript: appendError(next.transcript, frame),
        lastError: frame.errorInfo,
      };
      break;

    default:
      break;
  }

  if (!frame.ephemeral && frame.seq > 0) {
    next = { ...next, lastSeq: Math.max(next.lastSeq, frame.seq) };
  }

  return { state: { ...state, catalog, active: next, ui }, effects };
}

/**
 * Apply a batch of buffered replay frames as a single state transition.
 *
 * The transport holds frames until `session.subscribe` acknowledges the end
 * of the replay; this loop keeps intermediate states private to the reducer,
 * so React renders the finished transcript exactly once.
 */
function reduceFrames(state: AppState, frames: readonly ViewFrame[]): Reduced {
  let current = state;
  const effects: Effect[] = [];
  for (const frame of frames) {
    const reduced = reduceFrame(current, frame);
    current = reduced.state;
    if (reduced.effects.length > 0) effects.push(...reduced.effects);
  }
  return { state: current, effects };
}

export function reduce(state: AppState, action: Action): Reduced {
  switch (action.type) {
    case "gateway/status": {
      const changed = state.connection.status !== action.status;
      let ui = state.ui;
      if (changed) {
        let message: string | null = null;
        if (action.status === "reconnecting") message = "Reconnecting to the gateway…";
        else if (action.status === "closed") message = "Disconnected from the gateway.";
        else if (action.status === "error")
          message = `Connection error: ${action.error?.message ?? "unknown"}`;
        if (message) {
          ui = {
            ...ui,
            activity: pushActivity(
              ui.activity,
              makeActivity(action.status === "error" ? "error" : "connection", message),
            ),
          };
        }
      }
      return {
        state: {
          ...state,
          connection: {
            ...state.connection,
            status: action.status,
            error:
              action.error !== undefined
                ? action.error
                : action.status === "ready"
                  ? null
                  : state.connection.error,
            retryAt: action.retryAt ?? null,
          },
          ui,
        },
        effects: [],
      };
    }

    case "gateway/url":
      return {
        state: { ...state, connection: { ...state.connection, url: action.url } },
        effects: [],
      };

    case "gateway/hello": {
      const previous = state.connection.instanceId;
      const changedInstance = previous !== null && previous !== action.hello.instance_id;
      let active = state.active;
      if (changedInstance && active) active = resetForNewInstance(active);
      const message =
        previous === null
          ? `Connected (protocol ${action.hello.protocol}, ${action.hello.features.length} features).`
          : changedInstance
            ? "Gateway restarted; session history will be replayed."
            : "Reconnected to the gateway.";
      return {
        state: {
          ...state,
          connection: {
            ...state.connection,
            status: "ready",
            instanceId: action.hello.instance_id,
            protocol: action.hello.protocol,
            features: action.hello.features,
            limits: action.hello.limits,
            error: null,
            retryAt: null,
          },
          active,
          configuration: changedInstance
            ? INITIAL_CONFIGURATION
            : state.configuration,
          ui: {
            ...state.ui,
            activity: pushActivity(state.ui.activity, makeActivity("connection", message)),
          },
        },
        effects: [],
      };
    }

    case "gateway/frame":
      return reduceFrame(state, action.frame);

    case "gateway/commandFailed": {
      const ui = {
        ...state.ui,
        activity: pushActivity(
          state.ui.activity,
          makeActivity(
            "error",
            `${action.name} failed: ${action.error.code}: ${action.error.message}`,
          ),
        ),
        composer:
          action.draftText && state.ui.composer.trim() === ""
            ? action.draftText
            : state.ui.composer,
      };
      let uiState = ui;
      if (action.name === "artifact.read" && action.ref) {
        const artifact = ui.artifacts[action.ref];
        if (artifact) {
          uiState = {
            ...ui,
            artifacts: {
              ...ui.artifacts,
              [action.ref]: { ...artifact, loading: false, error: action.error },
            },
          };
        }
      }
      let catalog = state.catalog;
      if (action.name === "session.list") {
        catalog = { ...catalog, loading: false, error: action.error };
      }
      let plugins = state.plugins;
      if (action.name === "plugin.list") {
        plugins = { ...plugins, loading: false, error: action.error };
      } else if (action.name === "plugin.enable" || action.name === "plugin.disable") {
        plugins = { ...plugins, pendingId: null, error: action.error, lastAction: null };
      } else if (action.name === "plugin.set_allowed") {
        plugins = { ...plugins, applying: false, error: action.error, lastApply: null };
      }
      let active = state.active;
      if (active && (!action.sessionId || action.sessionId === active.id)) {
        if (action.name === "session.context") {
          active = { ...active, contextLoading: false, contextError: action.error };
        } else if (action.name === "session.tasks") {
          active = { ...active, tasksLoading: false };
        } else if (action.name === "mode.list") {
          active = { ...active, modesLoading: false };
        } else if (action.name === "session.subscribe") {
          // A dropped connection will resubscribe after the handshake; keep
          // the loading view up instead of flashing an empty session.
          if (action.error.code !== "E_CONNECTION_CLOSED") {
            active = { ...active, hydrating: false, lastError: action.error };
          }
        }
      }
      let configuration = state.configuration;
      if (action.name === "config.get") {
        // A failed refresh may leave a stale schema on screen; never let the
        // stale form write with the old revision.
        configuration = {
          ...configuration,
          loading: false,
          writable: false,
          error: action.error,
        };
      } else if (
        action.name === "config.set" ||
        action.name === "config.unset" ||
        action.name === "config.profile.set" ||
        action.name === "config.profile.delete" ||
        action.name === "config.profile.duplicate" ||
        action.name === "config.fallback.set" ||
        action.name === "config.fallback.delete" ||
        action.name === "config.fallback.move"
      ) {
        const current = action.error.context?.current_revision;
        configuration = {
          ...configuration,
          saving: false,
          previewing: false,
          preview: null,
          previewRevisionMismatch: false,
          error: action.error,
          conflict:
            action.error.code === "E_CONFIG_CONFLICT"
              ? {
                  currentRevision:
                    typeof current === "string" ? current : null,
                }
              : null,
        };
      }
      const effects: Effect[] = [];
      if (action.name === "session.subscribe") {
        effects.push({ type: "command", name: "session.list", args: sessionListArgs({}) });
        if (
          action.error.code === "E_SESSION_NOT_FOUND" &&
          (!action.sessionId || !active || action.sessionId === active.id)
        ) {
          active = null;
          effects.push({
            type: "persist",
            key: STORAGE_KEYS.lastSession,
            value: null,
          });
        }
      }
      return {
        state: {
          ...state,
          catalog,
          active,
          plugins,
          configuration,
          ui: uiState,
        },
        effects,
      };
    }

    case "gateway/unknownFrame":
      return {
        state: {
          ...state,
          ui: {
            ...state.ui,
            activity: pushActivity(
              state.ui.activity,
              makeActivity("debug", `Ignored unknown frame: ${action.name}`),
            ),
          },
        },
        effects: [],
      };

    case "catalog/loading":
      return {
        state: { ...state, catalog: { ...state.catalog, loading: true, error: null } },
        effects: [],
      };

    case "catalog/loaded": {
      let items = action.append ? state.catalog.items : [];
      for (const session of action.sessions) items = upsertSession(items, session);
      // A tab-restored session is opened before the catalogue arrives, so it
      // has no info yet. Fill it here; the workspace/file panels read
      // `active.info.workspace` and would otherwise stay empty.
      let active = state.active;
      const activeId = active?.id;
      if (active && activeId && !active.info) {
        const info = items.find((session) => session.session_id === activeId);
        if (info) active = { ...active, info };
      }
      return {
        state: {
          ...state,
          active,
          catalog: { ...state.catalog, items: sortSessions(items), loading: false, error: null },
        },
        effects: [],
      };
    }

    case "catalog/error":
      return {
        state: { ...state, catalog: { ...state.catalog, loading: false, error: action.error } },
        effects: [],
      };

    case "session/created": {
      const info = action.info;
      const effects: Effect[] = [
        {
          type: "command",
          name: "session.subscribe",
          args: sessionSubscribeArgs(0),
          sessionId: info.session_id,
        },
        { type: "command", name: "mode.list", args: {} },
        { type: "persist", key: STORAGE_KEYS.lastSession, value: info.session_id },
      ];
      if (info.workspace) {
        effects.push({ type: "persist", key: STORAGE_KEYS.lastWorkspace, value: info.workspace });
      }
      if (state.active) {
        effects.push({
          type: "command",
          name: "session.unsubscribe",
          args: {},
          sessionId: state.active.id,
        });
      }
      return {
        state: {
          ...state,
          active: initialActiveSession(info.session_id, info),
          catalog: { ...state.catalog, items: upsertSession(state.catalog.items, info) },
          ui: clearedFilePane({
            ...state.ui,
            composer: "",
            overlays: removeOverlay(state.ui.overlays, "new-session"),
          }),
        },
        effects,
      };
    }

    case "session/subscribed": {
      if (!state.active || action.sessionId !== state.active.id) {
        return noEffects(state);
      }
      const reduced = reduceFrames(state, action.frames);
      const nextActive = reduced.state.active;
      if (!nextActive) return reduced;
      // A gap inside the replay asks for another subscribe; keep the loading
      // view up until that follow-up replay is committed too.
      const resubscribe = reduced.effects.some(
        (effect) =>
          effect.type === "command" &&
          effect.name === "session.subscribe" &&
          effect.sessionId === nextActive.id,
      );
      return {
        state: {
          ...reduced.state,
          active: { ...nextActive, hydrating: resubscribe },
        },
        effects: reduced.effects,
      };
    }

    case "session/modesLoaded":
      if (!state.active) return noEffects(state);
      return {
        state: {
          ...state,
          active: { ...state.active, modes: action.modes, modesLoading: false },
        },
        effects: [],
      };

    case "session/contextLoading":
      if (!state.active) return noEffects(state);
      return {
        state: {
          ...state,
          active: { ...state.active, contextLoading: true, contextError: null },
        },
        effects: [],
      };

    case "session/contextLoaded":
      if (!state.active || action.context.session_id !== state.active.id) {
        return noEffects(state);
      }
      return {
        state: {
          ...state,
          active: {
            ...state.active,
            context: action.context,
            contextLoading: false,
            contextError: null,
          },
        },
        effects: [],
      };

    case "session/contextError":
      if (!state.active) return noEffects(state);
      return {
        state: {
          ...state,
          active: { ...state.active, contextLoading: false, contextError: action.error },
        },
        effects: [],
      };

    case "session/tasksLoading":
      if (!state.active) return noEffects(state);
      return {
        state: { ...state, active: { ...state.active, tasksLoading: true } },
        effects: [],
      };

    case "session/tasksLoaded":
      if (!state.active || (action.sessionId && action.sessionId !== state.active.id)) {
        return noEffects(state);
      }
      return {
        state: {
          ...state,
          active: {
            ...state.active,
            background: mergeTasks(state.active.background, action.tasks),
            tasksLoading: false,
          },
        },
        effects: [],
      };

    case "plugins/loaded":
      return {
        state: {
          ...state,
          plugins: {
            ...state.plugins,
            items: action.plugins,
            manage: action.manage,
            allow: action.allow,
            loading: false,
            error: null,
          },
        },
        effects: [],
      };

    case "plugins/managed": {
      // The command result is a partial summary (absent fields parse as
      // `undefined`); only overlay the fields it actually carries.
      const enabled = action.plugin.enabled === true;
      const items = state.plugins.items.map((plugin) =>
        plugin.id === action.plugin.id
          ? {
              ...plugin,
              enabled: action.plugin.enabled ?? plugin.enabled,
              state: action.plugin.state ?? plugin.state,
            }
          : plugin,
      );
      // `plugin.enable` / `plugin.disable` only persist the file-level allow.
      // Mirror that edit so the follow-up apply submits the new truth instead
      // of the stale pre-edit list (v0.4 post-extension, same as the TUI).
      const allow = withAllowed(state.plugins.allow, action.plugin.id, enabled);
      const canApply =
        action.changed &&
        state.connection.status === "ready" &&
        state.connection.features.includes("plugins.runtime") &&
        state.plugins.manage &&
        allow !== null;
      return {
        state: {
          ...state,
          plugins: {
            ...state.plugins,
            items,
            allow,
            applying: canApply,
            lastApply: null,
            pendingId: null,
            error: null,
            lastAction: {
              id: action.plugin.id,
              enabled,
              changed: action.changed,
              restartRequired: action.restartRequired,
            },
          },
        },
        effects:
          canApply && allow
            ? [
                {
                  type: "command",
                  name: "plugin.set_allowed",
                  args: pluginSetAllowedArgs(allow, "apply"),
                },
              ]
            : [],
      };
    }

    case "plugins/applied": {
      // `plugin.set_allowed` returns summaries only for ids that remain in
      // allow, and those summaries omit `enabled`. Merge into the catalogue
      // instead of replacing it, or disabled plugins would vanish until the
      // next `plugin.list`.
      const allowed = new Set(action.allow);
      const summaries = new Map(action.plugins.map((plugin) => [plugin.id, plugin]));
      const changes = new Map(action.changes.map((change) => [change.id, change]));
      const items: PluginSummary[] = state.plugins.items.map((plugin) => {
        const summary = summaries.get(plugin.id);
        const change = changes.get(plugin.id);
        return {
          ...plugin,
          enabled: allowed.has(plugin.id),
          state: summary?.state ?? change?.state ?? plugin.state,
          error: summary ? summary.error : plugin.error,
        };
      });
      for (const summary of action.plugins) {
        if (!items.some((plugin) => plugin.id === summary.id)) {
          items.push({ ...summary, enabled: allowed.has(summary.id) });
        }
      }
      return {
        state: {
          ...state,
          plugins: {
            ...state.plugins,
            items,
            allow: action.allow,
            applying: false,
            error: null,
            lastApply: {
              restartRequired: action.restartRequired,
              changes: action.changes,
            },
          },
        },
        effects: [],
      };
    }

    case "configuration/changed": {
      const cfg = state.configuration;
      const sameRevision = action.revision === cfg.revision;
      const next = {
        ...cfg,
        remoteRevision: action.revision,
        remoteChangedKeys: action.keys,
        // A same-revision push is our own write or a duplicate: recording it
        // is harmless, but it must not make an in-flight read look stale.
        remoteEpoch: sameRevision ? cfg.remoteEpoch : cfg.remoteEpoch + 1,
      };
      if (sameRevision || cfg.saving || cfg.loading || cfg.dirty) {
        return { state: { ...state, configuration: next }, effects: [] };
      }
      const sections = Object.keys(cfg.loadedSections);
      return {
        state: { ...state, configuration: next },
        effects: sections.length > 0
          ? [{
              type: "command",
              name: "config.get",
              args: configGetArgs(sections, false, true),
            }]
          : [],
      };
    }

    case "configuration/loading":
      return {
        state: {
          ...state,
          configuration: {
            ...state.configuration,
            loading: true,
            error: null,
          },
        },
        effects: [],
      };

    case "configuration/loaded": {
      const cfg = state.configuration;
      const sections = action.sections ?? null;
      const fieldsIncluded = action.fieldsIncluded !== false;
      const replace = fieldsIncluded && sections === null;
      const fields = fieldsIncluded
        ? (replace ? action.result.fields : { ...cfg.fields, ...action.result.fields })
        : cfg.fields;
      const runtimeScopes = fieldsIncluded
        ? (replace
            ? action.result.runtimeScopes
            : { ...cfg.runtimeScopes, ...action.result.runtimeScopes })
        : cfg.runtimeScopes;
      const loadedSections = { ...cfg.loadedSections };
      if (fieldsIncluded && sections) {
        for (const section of sections) {
          loadedSections[section] = {
            loading: false,
            error: null,
            loadedAtRevision: action.result.revision,
          };
        }
      } else if (fieldsIncluded && sections === null) {
        // A full refresh also refreshes every section that was loaded before.
        for (const key of Object.keys(loadedSections)) {
          loadedSections[key] = {
            ...loadedSections[key],
            loading: false,
            error: null,
            loadedAtRevision: action.result.revision,
          };
        }
        // A full fields response already contains every section, so mark all
        // schema sections loaded: opening one must not trigger a second read.
        for (const section of action.result.schema?.sections ?? []) {
          loadedSections[section.key] = {
            loading: false,
            error: null,
            loadedAtRevision: action.result.revision,
          };
        }
      }
      const epochAtRequest = action.remoteEpochAtRequest ?? cfg.remoteEpoch;
      const staleRead = epochAtRequest < cfg.remoteEpoch;
      const refreshSections = Object.keys(loadedSections);
      // A scoped read only clears the stale marker when it refreshed every
      // section that was loaded; otherwise another loaded section would keep
      // pre-push values with no banner (§6.5.3/§6.5.4).
      const refreshed = sections === null
        ? new Set(Object.keys(loadedSections))
        : new Set(sections);
      const coversAll = refreshSections.every((key) => refreshed.has(key));
      const clearRemote = !staleRead && coversAll;
      return {
        state: {
          ...state,
          configuration: {
            ...cfg,
            schema: mergeConfigSchema(cfg.schema, action.result.schema),
            fields,
            issues: action.result.issues,
            revision: action.result.revision,
            path: action.result.path,
            exists: action.result.exists,
            manage: action.result.manage,
            writable: action.result.writable,
            runtimeScopes,
            schemaComplete: action.result.schema !== null && sections === null
              ? true
              : cfg.schemaComplete,
            loadedSections,
            configInvalid: configIssuesInvalid(action.result.issues),
            loading: false,
            saving: false,
            error: null,
            conflict: null,
            remoteRevision: clearRemote ? null : cfg.remoteRevision,
            remoteChangedKeys: clearRemote ? [] : cfg.remoteChangedKeys,
          },
        },
        effects: staleRead && !cfg.dirty && refreshSections.length > 0
          ? [{
              type: "command",
              name: "config.get",
              args: configGetArgs(refreshSections, false, true),
            }]
          : [],
      };
    }

    case "configuration/sectionLoading": {
      const cfg = state.configuration;
      const previous = cfg.loadedSections[action.section];
      return {
        state: {
          ...state,
          configuration: {
            ...cfg,
            loadedSections: {
              ...cfg.loadedSections,
              [action.section]: {
                loading: true,
                error: null,
                loadedAtRevision: previous?.loadedAtRevision ?? "",
              },
            },
          },
        },
        effects: [],
      };
    }

    case "configuration/sectionLoaded": {
      const cfg = state.configuration;
      const result = action.result;
      return {
        state: {
          ...state,
          configuration: {
            ...cfg,
            schema: mergeConfigSchema(cfg.schema, result.schema),
            fields: { ...cfg.fields, ...result.fields },
            runtimeScopes: { ...cfg.runtimeScopes, ...result.runtimeScopes },
            issues: result.issues,
            revision: result.revision,
            path: result.path,
            exists: result.exists,
            manage: result.manage,
            writable: result.writable,
            loadedSections: {
              ...cfg.loadedSections,
              [action.section]: {
                loading: false,
                error: null,
                loadedAtRevision: result.revision,
              },
            },
            configInvalid: configIssuesInvalid(result.issues),
            error: null,
          },
        },
        effects: [],
      };
    }

    case "configuration/sectionError": {
      const cfg = state.configuration;
      const previous = cfg.loadedSections[action.section];
      return {
        state: {
          ...state,
          configuration: {
            ...cfg,
            loadedSections: {
              ...cfg.loadedSections,
              [action.section]: {
                loading: false,
                error: action.error,
                loadedAtRevision: previous?.loadedAtRevision ?? "",
              },
            },
          },
        },
        effects: [],
      };
    }

    case "configuration/draft": {
      const cfg = state.configuration;
      const draftSections = {
        ...cfg.draftSections,
        [action.section]: action.dirty,
      };
      return {
        state: {
          ...state,
          configuration: {
            ...cfg,
            draftSections,
            dirty: draftsDirty(draftSections),
          },
        },
        effects: [],
      };
    }

    case "configuration/previewed": {
      const cfg = state.configuration;
      return {
        state: {
          ...state,
          configuration: {
            ...cfg,
            preview: action.result,
            previewing: false,
            previewRevisionMismatch: action.revisionMismatch,
          },
        },
        effects: [],
      };
    }

    case "configuration/previewCleared": {
      const cfg = state.configuration;
      return {
        state: {
          ...state,
          configuration: {
            ...cfg,
            preview: null,
            previewing: false,
            previewRevisionMismatch: false,
          },
        },
        effects: [],
      };
    }

    case "configuration/saved": {
      const cfg = state.configuration;
      const draftSections = { ...cfg.draftSections };
      for (const section of action.sections ?? []) draftSections[section] = false;
      const loaded = Object.keys(cfg.loadedSections);
      const target = loaded.length > 0 ? loaded : ["agent.llm", "agent.profiles"];
      return {
        state: {
          ...state,
          configuration: {
            ...cfg,
            saving: false,
            lastSave: action.result,
            revision: action.result.revision,
            loading: true,
            draftSections,
            dirty: draftsDirty(draftSections),
            preview: null,
            previewing: false,
            previewRevisionMismatch: false,
            error: null,
            conflict: null,
          },
        },
        effects: [
          {
            type: "command",
            name: "config.get",
            args: configGetArgs(target, false, true),
          },
        ],
      };
    }

    case "configuration/error":
      return {
        state: {
          ...state,
          configuration: {
            ...state.configuration,
            loading: false,
            saving: false,
            error: action.error,
          },
        },
        effects: [],
      };

    case "configuration/conflict":
      return {
        state: {
          ...state,
          configuration: {
            ...state.configuration,
            conflict: { currentRevision: action.currentRevision },
          },
        },
        effects: [],
      };

    case "configuration/reset":
      return {
        state: { ...state, configuration: INITIAL_CONFIGURATION },
        effects: [],
      };

    case "session/childrenLoaded":
      if (
        !state.active ||
        (action.parentSessionId && action.parentSessionId !== state.active.id)
      ) {
        return noEffects(state);
      }
      return {
        state: { ...state, active: { ...state.active, childSessions: action.sessions } },
        effects: [],
      };

    case "file/open": {
      const existing = state.ui.openFiles.find(
        (file) => file.path === action.path && file.root === action.root,
      );
      if (existing) {
        // Re-clicking an errored tab retries it; otherwise just focus it.
        const retry = !existing.loading && existing.error !== undefined;
        const openFiles = retry
          ? state.ui.openFiles.map((file) =>
              file === existing
                ? { ...file, loading: true, error: undefined }
                : file,
            )
          : state.ui.openFiles;
        return {
          state: {
            ...state,
            ui: {
              ...state.ui,
              openFiles,
              activeFilePath: existing.path,
              filePaneOpen: true,
            },
          },
          effects: retry
            ? [{ type: "workspaceRead", root: action.root, path: action.path }]
            : [],
        };
      }
      const tab = {
        path: action.path,
        root: action.root,
        name: action.name,
        loading: true,
      };
      return {
        state: {
          ...state,
          ui: {
            ...state.ui,
            openFiles: [...state.ui.openFiles, tab],
            activeFilePath: tab.path,
            filePaneOpen: true,
          },
        },
        effects: [{ type: "workspaceRead", root: action.root, path: action.path }],
      };
    }

    case "file/loaded": {
      const index = state.ui.openFiles.findIndex(
        (file) => file.path === action.path && file.root === action.root,
      );
      if (index < 0) return noEffects(state);
      // A closed tab ignores its late read; a reopened one is the same path,
      // so the fresh result wins either way.
      const openFiles = state.ui.openFiles.map((file, position) =>
        position === index
          ? {
              ...file,
              loading: false,
              text: action.result.text,
              binary: action.result.binary,
              size: action.result.size,
              truncated: action.result.truncated,
              error: action.result.error,
            }
          : file,
      );
      return {
        state: { ...state, ui: { ...state.ui, openFiles } },
        effects: [],
      };
    }

    case "file/activate": {
      if (!state.ui.openFiles.some((file) => file.path === action.path)) {
        return noEffects(state);
      }
      return {
        state: {
          ...state,
          ui: {
            ...state.ui,
            activeFilePath: action.path,
            filePaneOpen: true,
          },
        },
        effects: [],
      };
    }

    case "file/close": {
      const index = state.ui.openFiles.findIndex(
        (file) => file.path === action.path,
      );
      if (index < 0) return noEffects(state);
      const openFiles = state.ui.openFiles.filter((_, position) => position !== index);
      let activeFilePath = state.ui.activeFilePath;
      if (activeFilePath === action.path) {
        activeFilePath = openFiles[Math.min(index, openFiles.length - 1)]?.path ?? null;
      }
      return {
        state: {
          ...state,
          ui: {
            ...state.ui,
            openFiles,
            activeFilePath,
            // An empty file sidebar is not worth keeping on screen.
            filePaneOpen: openFiles.length > 0 && state.ui.filePaneOpen,
          },
        },
        effects: [],
      };
    }

    case "file/paneToggle": {
      const filePaneOpen = !state.ui.filePaneOpen;
      const activeFilePath =
        filePaneOpen && state.ui.activeFilePath === null
          ? (state.ui.openFiles[0]?.path ?? null)
          : state.ui.activeFilePath;
      return {
        state: {
          ...state,
          ui: { ...state.ui, filePaneOpen, activeFilePath },
        },
        effects: [],
      };
    }

    case "ui/filePaneWidth": {
      const width = clamp(action.width, MIN_FILE_PANE_WIDTH, MAX_FILE_PANE_WIDTH);
      return {
        state: { ...state, ui: { ...state.ui, filePaneWidth: width } },
        effects: action.commit
          ? [{ type: "persist", key: STORAGE_KEYS.filePaneWidth, value: String(width) }]
          : [],
      };
    }

    case "ui/swapRightPanes": {
      const filePanePosition =
        state.ui.filePanePosition === "outer" ? "inner" : "outer";
      return {
        state: { ...state, ui: { ...state.ui, filePanePosition } },
        effects: [
          {
            type: "persist",
            key: STORAGE_KEYS.filePanePosition,
            value: filePanePosition,
          },
        ],
      };
    }

    case "intent/send": {
      if (!state.active) return noEffects(state);
      const text = action.text.trim();
      if (!text) return noEffects(state);
      const sessionId = state.active.id;
      return {
        state: { ...state, ui: { ...state.ui, composer: "" } },
        effects: [
          {
            type: "command",
            name: "user.message",
            args: { text },
            sessionId,
            restoreDraft: text,
          },
          { type: "persist", key: STORAGE_KEYS.draft(sessionId), value: "" },
        ],
      };
    }

    case "intent/interrupt":
      if (!state.active) return noEffects(state);
      return {
        state,
        effects: [
          {
            type: "command",
            name: "user.interrupt",
            args: { reason: "client stop" },
            sessionId: state.active.id,
          },
        ],
      };

    case "intent/approvalDecide":
      if (!state.active) return noEffects(state);
      return {
        state,
        effects: [
          {
            type: "command",
            name: "approval.decide",
            args: approvalDecideArgs(action.callId, action.decision, action.reason),
            sessionId: state.active.id,
          },
        ],
      };

    case "intent/taskStop":
      if (!state.active) return noEffects(state);
      return {
        state,
        effects: [
          {
            type: "command",
            name: "session.task.stop",
            args: taskStopArgs(action.taskId, "client stop"),
            sessionId: state.active.id,
          },
        ],
      };

    case "intent/modeSet":
      if (!state.active) return noEffects(state);
      return {
        state,
        effects: [
          {
            type: "command",
            name: "session.mode.set",
            args: modeSetArgs(action.modeName),
            sessionId: state.active.id,
          },
        ],
      };

    case "intent/createSession":
      return {
        state,
        effects: [
          {
            type: "command",
            name: "session.create",
            args: sessionCreateArgs({
              workspace: action.workspace,
              title: action.title,
              profile: action.profile,
            }),
          },
        ],
      };

    case "intent/openSession": {
      if (state.active?.id === action.sessionId) return noEffects(state);
      const active = initialActiveSession(action.sessionId, action.info ?? null);
      const effects: Effect[] = [
        {
          type: "command",
          name: "session.subscribe",
          args: sessionSubscribeArgs(0),
          sessionId: action.sessionId,
        },
        { type: "command", name: "mode.list", args: {} },
        { type: "persist", key: STORAGE_KEYS.lastSession, value: action.sessionId },
      ];
      if (action.info?.workspace) {
        effects.push({
          type: "persist",
          key: STORAGE_KEYS.lastWorkspace,
          value: action.info.workspace,
        });
      }
      if (state.active) {
        effects.push({
          type: "command",
          name: "session.unsubscribe",
          args: {},
          sessionId: state.active.id,
        });
      }
      return {
        state: {
          ...state,
          active,
          // The file preview is scoped to the session's workspace; switching
          // sessions must not leave the previous workspace's files on screen.
          ui: clearedFilePane({
            ...state.ui,
            composer: "",
            overlays: removeOverlay(state.ui.overlays, "session-picker"),
          }),
        },
        effects,
      };
    }

    case "intent/leaveSession": {
      if (!state.active) return noEffects(state);
      const sessionId = state.active.id;
      return {
        state: {
          ...state,
          active: null,
          ui: clearedFilePane({ ...state.ui, composer: "" }),
        },
        effects: [
          {
            type: "command",
            name: "session.unsubscribe",
            args: {},
            sessionId,
          },
          { type: "persist", key: STORAGE_KEYS.lastSession, value: null },
        ],
      };
    }

    case "intent/refreshSessions": {
      const workspace = action.workspace;
      return {
        state: { ...state, catalog: { ...state.catalog, loading: true, error: null } },
        effects: [
          {
            type: "command",
            name: "session.list",
            args: sessionListArgs({ workspace: workspace || undefined }),
          },
        ],
      };
    }

    case "intent/refreshContext":
      if (!state.active) return noEffects(state);
      return {
        state: {
          ...state,
          active: { ...state.active, contextLoading: true, contextError: null },
        },
        effects: [
          {
            type: "command",
            name: "session.context",
            // The sidebar only renders counters; the full provider plan can
            // exceed the gateway's single-frame budget.
            args: { include_messages: false },
            sessionId: state.active.id,
          },
        ],
      };

    case "intent/refreshTasks":
      if (!state.active) return noEffects(state);
      return {
        state: { ...state, active: { ...state.active, tasksLoading: true } },
        effects: [
          {
            type: "command",
            name: "session.tasks",
            args: {},
            sessionId: state.active.id,
          },
        ],
      };

    case "intent/refreshModes":
      if (!state.active) return noEffects(state);
      return {
        state: { ...state, active: { ...state.active, modesLoading: true } },
        effects: [{ type: "command", name: "mode.list", args: {} }],
      };

    case "intent/refreshPlugins":
      // `plugin.list` is gateway-wide (no session_id), so settings can load it
      // before the user opens any session.
      return {
        state: {
          ...state,
          plugins: { ...state.plugins, loading: true, error: null },
        },
        effects: [{ type: "command", name: "plugin.list", args: {} }],
      };

    case "intent/pluginSetEnabled": {
      const allowed =
        state.connection.status === "ready" &&
        state.plugins.manage &&
        state.connection.features.includes("plugins.manage");
      if (!allowed) return noEffects(state);
      return {
        state: {
          ...state,
          plugins: {
            ...state.plugins,
            pendingId: action.id,
            error: null,
            lastAction: null,
            lastApply: null,
          },
        },
        effects: [
          {
            type: "command",
            name: action.enabled ? "plugin.enable" : "plugin.disable",
            args: { id: action.id },
          },
        ],
      };
    }

    case "intent/applyPlugins": {
      const allow = state.plugins.allow;
      const canApply =
        state.connection.status === "ready" &&
        state.connection.features.includes("plugins.runtime") &&
        state.plugins.manage &&
        allow !== null;
      if (!canApply || !allow) return noEffects(state);
      return {
        state: {
          ...state,
          plugins: { ...state.plugins, applying: true, error: null, lastApply: null },
        },
        effects: [
          {
            type: "command",
            name: "plugin.set_allowed",
            args: pluginSetAllowedArgs(allow, "apply"),
          },
        ],
      };
    }

    case "intent/refreshConfig":
      // `config.get` is gateway-wide, so settings can load it before a session.
      return {
        state: {
          ...state,
          configuration: {
            ...state.configuration,
            loading: true,
            error: null,
            conflict: null,
          },
        },
        effects: [
          {
            type: "command",
            name: "config.get",
            args: configGetArgs(
              action.sections,
              action.includeSchema ?? true,
              action.includeFields ?? true,
            ),
          },
        ],
      };

    case "intent/loadSection": {
      const cfg = state.configuration;
      const previous = cfg.loadedSections[action.section];
      return {
        state: {
          ...state,
          configuration: {
            ...cfg,
            loadedSections: {
              ...cfg.loadedSections,
              [action.section]: {
                loading: true,
                error: null,
                loadedAtRevision: previous?.loadedAtRevision ?? "",
              },
            },
          },
        },
        effects: [
          {
            type: "command",
            name: "config.get",
            args: configGetArgs([action.section], false, true),
          },
        ],
      };
    }

    case "intent/configDraft":
      return {
        state: {
          ...state,
          configuration: {
            ...state.configuration,
            draftSections: {
              ...state.configuration.draftSections,
              [action.section]: action.dirty,
            },
            dirty: draftsDirty({
              ...state.configuration.draftSections,
              [action.section]: action.dirty,
            }),
          },
        },
        effects: [],
      };

    case "intent/previewConfig": {
      if (!configSaveAllowed(state)) return noEffects(state);
      return {
        state: {
          ...state,
          configuration: {
            ...state.configuration,
            previewing: true,
            preview: null,
            previewRevisionMismatch: false,
            error: null,
          },
        },
        effects: [
          {
            type: "command",
            name: "config.set",
            args: configSetArgs(
              action.changes,
              state.configuration.revision,
              false,
            ),
          },
        ],
      };
    }

    case "intent/saveConfig": {
      const configuration = state.configuration;
      const allowed =
        state.connection.status === "ready" &&
        configuration.writable &&
        (configuration.revision !== "" || !configuration.exists) &&
        Object.keys(action.changes).length > 0;
      if (!allowed) return noEffects(state);
      return {
        state: {
          ...state,
          configuration: {
            ...configuration,
            saving: true,
            error: null,
            lastSave: null,
            conflict: null,
          },
        },
        effects: [
          {
            type: "command",
            name: "config.set",
            args: configSetArgs(action.changes, action.expectedRevision),
          },
        ],
      };
    }

    case "intent/unsetConfig": {
      const configuration = state.configuration;
      const allowed =
        state.connection.status === "ready" &&
        configuration.writable &&
        (configuration.revision !== "" || !configuration.exists) &&
        action.keys.length > 0;
      if (!allowed) return noEffects(state);
      return {
        state: {
          ...state,
          configuration: {
            ...configuration,
            saving: true,
            error: null,
            lastSave: null,
            conflict: null,
          },
        },
        effects: [
          {
            type: "command",
            name: "config.unset",
            args: configUnsetArgs(action.keys, action.expectedRevision),
          },
        ],
      };
    }

    case "intent/saveProfile":
      if (!configSaveAllowed(state)) return noEffects(state);
      return {
        state: savingConfiguration(state),
        effects: [
          {
            type: "command",
            name: "config.profile.set",
            args: configProfileSetArgs(
              action.name,
              action.values,
              state.configuration.revision,
            ),
          },
        ],
      };

    case "intent/deleteProfile":
      if (!configSaveAllowed(state)) return noEffects(state);
      return {
        state: savingConfiguration(state),
        effects: [
          {
            type: "command",
            name: "config.profile.delete",
            args: configProfileDeleteArgs(
              action.name,
              state.configuration.revision,
              action.confirmInUse === true,
            ),
          },
        ],
      };

    case "intent/duplicateProfile":
      if (!configSaveAllowed(state)) return noEffects(state);
      return {
        state: savingConfiguration(state),
        effects: [
          {
            type: "command",
            name: "config.profile.duplicate",
            args: configProfileDuplicateArgs(
              action.from,
              action.name,
              state.configuration.revision,
            ),
          },
        ],
      };

    case "intent/saveFallback":
      if (!configSaveAllowed(state)) return noEffects(state);
      return {
        state: savingConfiguration(state),
        effects: [
          {
            type: "command",
            name: "config.fallback.set",
            args: configFallbackSetArgs(
              action.values,
              state.configuration.revision,
              action.index,
            ),
          },
        ],
      };

    case "intent/deleteFallback":
      if (!configSaveAllowed(state)) return noEffects(state);
      return {
        state: savingConfiguration(state),
        effects: [
          {
            type: "command",
            name: "config.fallback.delete",
            args: configFallbackDeleteArgs(
              action.index,
              state.configuration.revision,
            ),
          },
        ],
      };

    case "intent/moveFallback":
      if (!configSaveAllowed(state)) return noEffects(state);
      return {
        state: savingConfiguration(state),
        effects: [
          {
            type: "command",
            name: "config.fallback.move",
            args: configFallbackMoveArgs(
              action.from,
              action.to,
              state.configuration.revision,
            ),
          },
        ],
      };

    case "intent/refreshChildren":
      if (!state.active) return noEffects(state);
      return {
        state,
        effects: [
          {
            type: "command",
            name: "session.children",
            args: childrenArgs(),
            sessionId: state.active.id,
          },
        ],
      };

    case "intent/readArtifact": {
      const offset = action.offset ?? 0;
      const existing = state.ui.artifacts[action.ref];
      const artifact =
        offset === 0 || !existing
          ? {
              ref: action.ref,
              chunks: [] as string[],
              size: 0,
              eof: false,
              loading: true,
              error: null,
            }
          : { ...existing, loading: true };
      return {
        state: {
          ...state,
          ui: {
            ...state.ui,
            artifacts: { ...state.ui.artifacts, [action.ref]: artifact },
          },
        },
        effects: [
          {
            type: "command",
            name: "artifact.read",
            args: artifactReadArgs(action.ref, offset, action.limit ?? ARTIFACT_CHUNK_BYTES),
            sessionId: state.active?.id,
          },
        ],
      };
    }

    case "intent/resume":
      if (!state.active) return noEffects(state);
      return {
        state,
        effects: [
          {
            type: "command",
            name: "agent.resume",
            args: {},
            sessionId: state.active.id,
          },
        ],
      };

    case "ui/page":
      return {
        state: { ...state, ui: { ...state.ui, page: action.page } },
        effects: [],
      };

    case "ui/railWidth": {
      const width =
        action.side === "left"
          ? clamp(action.width, MIN_LEFT_RAIL_WIDTH, MAX_LEFT_RAIL_WIDTH)
          : clamp(action.width, MIN_RIGHT_RAIL_WIDTH, MAX_RIGHT_RAIL_WIDTH);
      const ui: typeof state.ui = {
        ...state.ui,
        leftRailWidth: action.side === "left" ? width : state.ui.leftRailWidth,
        rightRailWidth: action.side === "right" ? width : state.ui.rightRailWidth,
      };
      const effects: Effect[] = action.commit
        ? [
            {
              type: "persist",
              key:
                action.side === "left"
                  ? STORAGE_KEYS.leftRailWidth
                  : STORAGE_KEYS.rightRailWidth,
              value: String(width),
            },
          ]
        : [];
      return { state: { ...state, ui }, effects };
    }

    case "ui/theme":
      return {
        state: { ...state, ui: { ...state.ui, theme: action.theme } },
        effects: [{ type: "persist", key: STORAGE_KEYS.theme, value: action.theme }],
      };

    case "ui/locale":
      return {
        state: { ...state, ui: { ...state.ui, locale: action.locale } },
        effects: [{ type: "persist", key: STORAGE_KEYS.locale, value: action.locale }],
      };

    case "ui/toggleLeftRail":
      return {
        state: { ...state, ui: { ...state.ui, leftRailOpen: !state.ui.leftRailOpen } },
        effects: [],
      };

    case "ui/toggleRightRail":
      return {
        state: {
          ...state,
          ui: { ...state.ui, rightRailOpen: !state.ui.rightRailOpen },
        },
        effects: [
          {
            type: "persist",
            key: STORAGE_KEYS.rightRail,
            value: String(!state.ui.rightRailOpen),
          },
        ],
      };

    case "ui/togglePanel": {
      const openPanels = toggleOpenPanel(state.ui.openPanels, action.panelId);
      return {
        state: { ...state, ui: { ...state.ui, openPanels } },
        effects: [
          {
            type: "persist",
            key: STORAGE_KEYS.openPanels,
            value: JSON.stringify(openPanels),
          },
        ],
      };
    }

    case "ui/panelMode":
      return {
        state: { ...state, ui: { ...state.ui, panelMode: action.mode } },
        effects: [
          { type: "persist", key: STORAGE_KEYS.panelMode, value: action.mode },
        ],
      };

    case "ui/selectPanel":
      return {
        state: {
          ...state,
          ui: {
            ...state.ui,
            rightRailOpen: true,
            activePanelId: action.panelId,
            openPanels: { ...state.ui.openPanels, [action.panelId]: true },
          },
        },
        effects: [],
      };

    case "ui/openOverlay":
      return {
        state: {
          ...state,
          ui: { ...state.ui, overlays: [...state.ui.overlays, action.overlay] },
        },
        effects: [],
      };

    case "ui/closeOverlay": {
      const overlays = action.kind
        ? removeOverlay(state.ui.overlays, action.kind)
        : state.ui.overlays.slice(0, -1);
      return { state: { ...state, ui: { ...state.ui, overlays } }, effects: [] };
    }

    case "ui/composer": {
      const effects: Effect[] =
        state.active && action.text !== state.ui.composer
          ? [
              {
                type: "persist",
                key: STORAGE_KEYS.draft(state.active.id),
                value: action.text,
              },
            ]
          : [];
      return {
        state: { ...state, ui: { ...state.ui, composer: action.text } },
        effects,
      };
    }

    case "ui/activityToggle":
      return {
        state: {
          ...state,
          ui: { ...state.ui, activityOpen: !state.ui.activityOpen },
        },
        effects: [],
      };

    case "ui/activityClear":
      return {
        state: { ...state, ui: { ...state.ui, activity: [] } },
        effects: [],
      };

    case "ui/clearError":
      return {
        state: {
          ...state,
          connection: { ...state.connection, error: null },
          active: state.active ? { ...state.active, lastError: null } : null,
        },
        effects: [],
      };

    default:
      return noEffects(state);
  }
}
