/**
 * Wires the gateway client to the store.
 *
 * The reducer emits effects; this controller performs them. Command results
 * are mapped back into typed store actions. No UI component talks to the
 * transport directly.
 */

import {
  COMMAND_TIMEOUT_MS,
  DEFAULT_GATEWAY_URL,
  STORAGE_KEYS,
  SUBSCRIBE_TIMEOUT_MS,
} from "../config";
import {
  parseConfigGetResult,
  parseConfigSetResult,
  parseContextInfo,
  parseModeInfo,
  parsePluginChanges,
  parsePluginSummary,
  parseSessionInfo,
  sessionSubscribeArgs,
  type ConfigSetResult,
  type ContextInfo,
  type SessionInfo,
} from "../protocol/commands";
import {
  asArray,
  asNumber,
  asRecord,
  asString,
  asStringArray,
  newEnvelopeId,
  type ErrorInfo,
  type FrameEnvelope,
  type ResultEnvelope,
} from "../protocol/envelope";
import { GatewayCommandError, localError } from "../protocol/errors";
import { parseBackgroundTask, parseFrame } from "../protocol/frames";
import { ConfigPushCoalescer } from "./configPush";
import { configKeySection } from "../state/configuration";
import { store } from "../state/instance";
import { readLastSession, writeLastSession } from "../state/tabSession";
import type { CommandEffect, Effect } from "../state/types";
import { workspaceProvider } from "../workspace";
import { GatewayClient, type ClientStatus } from "./client";
import { HydrationBuffer, isHydrationFrame } from "./hydration";

function defined<T>(value: T | null): value is T {
  return value !== null;
}

export class GatewayController {
  private readonly client: GatewayClient;
  private readonly unknownFrames = new Set<string>();
  private readonly persistTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private readonly hydration = new HydrationBuffer();
  /** Monotonic id for subscribe commands; stale results must not flush a newer replay. */
  private subscribeToken = 0;
  /** `config.changed` bursts collapse into one 100 ms dispatch (§6.5.2). */
  private readonly configPushes = new ConfigPushCoalescer((push) => {
    store.dispatch({
      type: "configuration/changed",
      revision: push.revision,
      keys: push.keys,
    });
  });
  /** `remoteEpoch` when the matching `config.get` was issued. */
  private readonly configEpochAtRequest = new WeakMap<object, number>();

  constructor() {
    this.client = new GatewayClient({
      onStatus: (status, detail) => this.handleStatus(status, detail),
      onHello: (hello, reconnected) => this.handleHello(hello, reconnected),
      onFrame: (frame) => this.handleFrame(frame),
      onUnknown: () => this.noteUnknown("unparseable envelope"),
    });
    store.setEffectHandler((effects) => this.runEffects(effects));
  }

  // -- connection ---------------------------------------------------------

  connect(url?: string): void {
    const target =
      (url && url.trim()) || store.getState().connection.url || DEFAULT_GATEWAY_URL;
    store.dispatch({ type: "gateway/url", url: target });
    this.persist(STORAGE_KEYS.gatewayUrl, target);
    this.client.connect(target);
  }

  disconnect(): void {
    this.configPushes.clear();
    this.client.close();
  }

  private handleStatus(
    status: ClientStatus,
    detail?: { error?: string; retryAt?: number },
  ): void {
    if (status !== "ready") this.configPushes.clear();
    store.dispatch({
      type: "gateway/status",
      status,
      error: detail?.error ? localError("E_CONNECTION", detail.error) : undefined,
      retryAt: detail?.retryAt ?? null,
    });
  }

  private handleHello(
    hello: { instance_id: string; protocol: number; features: string[]; limits: Record<string, number> },
    _reconnected: boolean,
  ): void {
    // Any in-flight replay belongs to the previous socket.
    this.hydration.clear();
    this.configPushes.clear();
    store.dispatch({
      type: "gateway/hello",
      hello: {
        instance_id: hello.instance_id,
        protocol: hello.protocol,
        features: hello.features,
        limits: hello.limits,
      },
    });
    this.refreshSessions();

    // Resubscribe on every handshake. A gateway restart resets the stored
    // cursor to 0, so this also performs a full replay.
    const active = store.getState().active;
    if (active) {
      void this.executeCommand({
        type: "command",
        name: "session.subscribe",
        args: sessionSubscribeArgs(active.lastSeq),
        sessionId: active.id,
      });
      return;
    }

    // A refresh (or an in-place reconnect that lost the reducer) restores the
    // session this tab had open. `sessionStorage` is tab-scoped, so a new tab
    // or a closed tab has no record to restore.
    const lastSession = readLastSession();
    if (lastSession) {
      store.dispatch({
        type: "intent/openSession",
        sessionId: lastSession,
        info: null,
      });
    }
  }

  private handleFrame(frame: FrameEnvelope): void {
    const parsed = parseFrame(frame);
    if (!parsed) {
      this.noteUnknown(frame.frame);
      return;
    }
    // Configuration pushes are a gateway-wide notification, not a session
    // fact: coalesce them here so the reducer never sees an unknown frame.
    if (parsed.kind === "config.changed") {
      if (!store.getState().connection.features.includes("config.push")) return;
      this.configPushes.push({ revision: parsed.revision, keys: parsed.keys });
      return;
    }
    // Every replayed frame arrives before the `session.subscribe` result.
    // Hold them until that result so the transcript and approvals appear in
    // one render instead of replaying event by event.
    const active = store.getState().active;
    if (active && isHydrationFrame(active, parsed)) {
      this.hydration.push(active.id, parsed);
      return;
    }
    store.dispatch({ type: "gateway/frame", frame: parsed });
  }

  private noteUnknown(name: string): void {
    if (this.unknownFrames.has(name)) return;
    this.unknownFrames.add(name);
    store.dispatch({ type: "gateway/unknownFrame", name });
  }

  // -- effects ------------------------------------------------------------

  private runEffects(effects: Effect[]): void {
    for (const effect of effects) {
      switch (effect.type) {
        case "command":
          void this.executeCommand(effect);
          break;
        case "connect":
          this.connect(effect.url);
          break;
        case "disconnect":
          this.disconnect();
          break;
        case "persist":
          this.persist(effect.key, effect.value);
          break;
        case "workspaceRead":
          void this.readWorkspaceFile(effect.root, effect.path);
          break;
      }
    }
  }

  private async readWorkspaceFile(root: string, path: string): Promise<void> {
    const result = await workspaceProvider.read(root, path);
    store.dispatch({ type: "file/loaded", root, path, result });
  }

  private async executeCommand(effect: CommandEffect): Promise<void> {
    // Only session-scoped effects carry a session id. Commands such as
    // `session.list`, `mode.list` and `plugin.list` reject a session id.
    const sessionId = effect.sessionId;
    const subscribeToken =
      effect.name === "session.subscribe" ? ++this.subscribeToken : undefined;
    const args: Record<string, unknown> = { ...effect.args };
    if (effect.name === "config.get") {
      // Snapshot the push epoch so the result can tell whether a newer
      // `config.changed` arrived while this read was in flight (§6.5.3).
      this.configEpochAtRequest.set(
        args, store.getState().configuration.remoteEpoch,
      );
    }
    if (effect.name === "user.message" && typeof args.client_message_id !== "string") {
      args.client_message_id = newEnvelopeId("msg");
    }
    const active = store.getState().active;
    if (
      subscribeToken !== undefined &&
      sessionId &&
      active?.id === sessionId &&
      active.hydrating
    ) {
      this.hydration.begin(sessionId);
    }
    try {
      const result = await this.client.call(effect.name, args, {
        sessionId,
        timeoutMs:
          effect.timeoutMs ??
          (effect.name === "session.subscribe"
            ? SUBSCRIBE_TIMEOUT_MS
            : COMMAND_TIMEOUT_MS),
      });
      this.consumeResult(effect.name, result, sessionId, subscribeToken, args);
    } catch (error) {
      const staleSubscribe =
        subscribeToken !== undefined && subscribeToken !== this.subscribeToken;
      if (subscribeToken !== undefined && sessionId && !staleSubscribe) {
        this.hydration.discard(sessionId);
      }
      // A newer subscribe supersedes this one; its result owns the buffer.
      if (staleSubscribe) return;
      const info =
        error instanceof GatewayCommandError
          ? error.errorInfo
          : localError("E_CLIENT", error instanceof Error ? error.message : String(error));
      if (effect.name === "config.get" && Array.isArray(args.sections)) {
        for (const section of args.sections) {
          if (typeof section === "string") {
            store.dispatch({
              type: "configuration/sectionError",
              section,
              error: info,
            });
          }
        }
      }
      store.dispatch({
        type: "gateway/commandFailed",
        name: effect.name,
        error: info,
        draftText: effect.restoreDraft,
        ref: typeof args.ref === "string" ? args.ref : undefined,
        sessionId,
      });
    }
  }

  private dispatchConfigSaved(
    result: ConfigSetResult,
    name?: string,
  ): void {
    const state = store.getState();
    const sections = new Set<string>();
    if (name?.startsWith("config.profile.") || name?.startsWith("config.fallback.")) {
      sections.add("agent.llm");
      sections.add("agent.profiles");
    } else {
      for (const change of result.changes) {
        sections.add(configKeySection(change.key, state.configuration.schema));
      }
    }
    store.dispatch({
      type: "configuration/saved",
      result,
      sections: [...sections],
    });
  }

  private consumeResult(
    name: string,
    result: ResultEnvelope,
    sessionId?: string,
    subscribeToken?: number,
    args: Record<string, unknown> = {},
  ): void {
    if (
      name === "session.subscribe" &&
      subscribeToken !== undefined &&
      subscribeToken !== this.subscribeToken
    ) {
      // A newer replay replaced this one; do not flush its partial buffer.
      return;
    }
    const payload = result.result;
    switch (name) {
      case "session.list": {
        const sessions = asArray(payload.sessions)
          .map(parseSessionInfo)
          .filter(defined);
        store.dispatch({
          type: "catalog/loaded",
          sessions,
          limit: asNumber(payload.limit) ?? 0,
          offset: asNumber(payload.offset) ?? 0,
          append: false,
        });
        break;
      }
      case "session.create": {
        const info = parseSessionInfo(payload.session);
        if (info) {
          store.dispatch({ type: "session/created", info });
        } else {
          store.dispatch({
            type: "gateway/commandFailed",
            name,
            error: localError("E_PROTOCOL", "session.create returned no session"),
          });
        }
        break;
      }
      case "session.subscribe": {
        const active = store.getState().active;
        if (!active || (sessionId !== undefined && sessionId !== active.id)) {
          if (sessionId !== undefined) this.hydration.discard(sessionId);
          break;
        }
        store.dispatch({
          type: "session/subscribed",
          sessionId: active.id,
          snapshotSeq: asNumber(payload.snapshot_seq) ?? 0,
          frames: this.hydration.take(active.id),
        });
        break;
      }
      case "session.context": {
        const context = parseContextInfo(payload.context);
        if (context) {
          store.dispatch({ type: "session/contextLoaded", context });
        }
        break;
      }
      case "session.tasks": {
        const tasks = asArray(payload.tasks).map((entry) =>
          parseBackgroundTask(asRecord(entry)),
        );
        store.dispatch({
          type: "session/tasksLoaded",
          sessionId: asString(payload.session_id) ?? "",
          tasks,
        });
        break;
      }
      case "session.children": {
        const sessions = asArray(payload.sessions)
          .map(parseSessionInfo)
          .filter(defined);
        store.dispatch({
          type: "session/childrenLoaded",
          parentSessionId: asString(payload.parent_session_id) ?? "",
          sessions,
        });
        break;
      }
      case "mode.list": {
        const modes = asArray(payload.modes).map(parseModeInfo).filter(defined);
        store.dispatch({ type: "session/modesLoaded", modes });
        break;
      }
      case "plugin.list": {
        const plugins = asArray(payload.plugins).map(parsePluginSummary).filter(defined);
        store.dispatch({
          type: "plugins/loaded",
          plugins,
          manage: payload.manage === true,
          // The field is omitted when the host cannot read the file config.
          allow: Array.isArray(payload.allow) ? asStringArray(payload.allow) : null,
        });
        break;
      }
      case "plugin.enable":
      case "plugin.disable": {
        const plugin = parsePluginSummary(payload.plugin);
        if (plugin) {
          store.dispatch({
            type: "plugins/managed",
            plugin,
            changed: payload.changed === true,
            restartRequired: payload.restart_required === true,
          });
        }
        break;
      }
      case "plugin.set_allowed": {
        store.dispatch({
          type: "plugins/applied",
          allow: asStringArray(payload.allow),
          plugins: asArray(payload.plugins).map(parsePluginSummary).filter(defined),
          restartRequired: payload.restart_required === true,
          changes: parsePluginChanges(payload.changes),
        });
        break;
      }
      case "config.get": {
        const result = parseConfigGetResult(payload);
        if (result) {
          const sections = Array.isArray(args.sections)
            ? args.sections.filter((value): value is string => typeof value === "string")
            : null;
          store.dispatch({
            type: "configuration/loaded",
            result,
            sections,
            fieldsIncluded: args.include_fields !== false,
            remoteEpochAtRequest:
              this.configEpochAtRequest.get(args) ??
              store.getState().configuration.remoteEpoch,
          });
        }
        break;
      }
      case "config.set": {
        const result = parseConfigSetResult(payload);
        if (result) {
          if (args.persist === false) {
            store.dispatch({
              type: "configuration/previewed",
              result,
              revisionMismatch:
                typeof args.expected_revision === "string" &&
                result.revision !== args.expected_revision,
            });
          } else {
            this.dispatchConfigSaved(result);
          }
        }
        break;
      }
      case "config.unset":
      case "config.profile.set":
      case "config.profile.delete":
      case "config.profile.duplicate":
      case "config.fallback.set":
      case "config.fallback.delete":
      case "config.fallback.move": {
        const result = parseConfigSetResult(payload);
        if (result) this.dispatchConfigSaved(result, name);
        break;
      }
      default:
        break;
    }
  }

  private persist(key: string, value: string | null): void {
    const write = () => {
      if (key === STORAGE_KEYS.lastSession) {
        // Tab-scoped: refresh restores, a new/closed tab starts clean.
        writeLastSession(value);
        return;
      }
      try {
        if (value === null) localStorage.removeItem(key);
        else localStorage.setItem(key, value);
      } catch {
        // Preferences are best-effort only.
      }
    };
    if (key.startsWith("linteum.draft.")) {
      const existing = this.persistTimers.get(key);
      if (existing) clearTimeout(existing);
      this.persistTimers.set(
        key,
        setTimeout(() => {
          this.persistTimers.delete(key);
          write();
        }, 250),
      );
      return;
    }
    write();
  }

  private readDraft(sessionId: string): string {
    try {
      return localStorage.getItem(STORAGE_KEYS.draft(sessionId)) ?? "";
    } catch {
      return "";
    }
  }

  // -- user actions -------------------------------------------------------

  sendMessage(text: string): void {
    store.dispatch({ type: "intent/send", text });
  }

  interrupt(): void {
    store.dispatch({ type: "intent/interrupt" });
  }

  decideApproval(callId: string, decision: "grant" | "deny", reason?: string): void {
    store.dispatch({ type: "intent/approvalDecide", callId, decision, reason });
  }

  stopTask(taskId: string): void {
    store.dispatch({ type: "intent/taskStop", taskId });
  }

  setMode(modeName: string): void {
    store.dispatch({ type: "intent/modeSet", modeName });
  }

  createSession(workspace: string, title?: string, profile?: string): void {
    this.hydration.clear();
    store.dispatch({ type: "intent/createSession", workspace, title, profile });
  }

  openSession(sessionId: string, info?: SessionInfo | null): void {
    if (store.getState().active?.id === sessionId) return;
    this.hydration.clear();
    store.dispatch({ type: "intent/openSession", sessionId, info: info ?? null });
    const draft = this.readDraft(sessionId);
    if (draft) {
      store.dispatch({ type: "ui/composer", text: draft });
    }
  }

  leaveSession(): void {
    this.hydration.clear();
    store.dispatch({ type: "intent/leaveSession" });
  }

  refreshSessions(workspace?: string): void {
    store.dispatch({ type: "intent/refreshSessions", workspace });
  }

  refreshContext(): void {
    store.dispatch({ type: "intent/refreshContext" });
  }

  refreshTasks(): void {
    store.dispatch({ type: "intent/refreshTasks" });
  }

  refreshModes(): void {
    store.dispatch({ type: "intent/refreshModes" });
  }

  refreshPlugins(): void {
    store.dispatch({ type: "intent/refreshPlugins" });
  }

  refreshConfig(
    sections?: string[],
    includeSchema = true,
    includeFields = true,
  ): void {
    store.dispatch({
      type: "intent/refreshConfig",
      sections,
      includeSchema,
      includeFields,
    });
  }

  loadConfigSection(section: string): void {
    store.dispatch({ type: "intent/loadSection", section });
  }

  previewConfig(section: string, changes: Record<string, unknown>): void {
    store.dispatch({ type: "intent/previewConfig", section, changes });
  }

  saveConfig(changes: Record<string, unknown>, expectedRevision: string): void {
    store.dispatch({ type: "intent/saveConfig", changes, expectedRevision });
  }

  unsetConfig(keys: string[], expectedRevision: string): void {
    store.dispatch({ type: "intent/unsetConfig", keys, expectedRevision });
  }

  saveProfile(name: string, values: Record<string, unknown>): void {
    store.dispatch({ type: "intent/saveProfile", name, values });
  }

  deleteProfile(name: string, confirmInUse?: boolean): void {
    store.dispatch({ type: "intent/deleteProfile", name, confirmInUse });
  }

  duplicateProfile(from: string, name: string): void {
    store.dispatch({ type: "intent/duplicateProfile", from, name });
  }

  saveFallback(values: Record<string, unknown>, index?: number): void {
    store.dispatch({ type: "intent/saveFallback", values, index });
  }

  deleteFallback(index: number): void {
    store.dispatch({ type: "intent/deleteFallback", index });
  }

  moveFallback(from: number, to: number): void {
    store.dispatch({ type: "intent/moveFallback", from, to });
  }

  setPluginEnabled(id: string, enabled: boolean): void {
    store.dispatch({ type: "intent/pluginSetEnabled", id, enabled });
  }

  /** Apply the saved allow list to the running gateway (`mode="apply"`). */
  applyPlugins(): void {
    store.dispatch({ type: "intent/applyPlugins" });
  }

  refreshChildren(): void {
    store.dispatch({ type: "intent/refreshChildren" });
  }

  readArtifact(ref: string): void {
    if (!store.getState().ui.artifacts[ref]) {
      store.dispatch({ type: "intent/readArtifact", ref });
    }
  }

  /**
   * Read-only `session.context` for a child session. The response carries one
   * bounded page of the provider-facing plan (system blocks + the child's
   * messages); callers page with `next_offset` until `has_more` is false.
   */
  async loadChildContext(
    sessionId: string,
    offset = 0,
    limit = 100,
  ): Promise<{
    context: ContextInfo | null;
    error: ErrorInfo | null;
  }> {
    try {
      const result = await this.client.call(
        "session.context",
        { offset, limit },
        { sessionId, timeoutMs: COMMAND_TIMEOUT_MS },
      );
      const context = parseContextInfo(result.result.context);
      return context
        ? { context, error: null }
        : {
            context: null,
            error: localError("E_PROTOCOL", "session.context returned no context"),
          };
    } catch (error) {
      return {
        context: null,
        error:
          error instanceof GatewayCommandError
            ? error.errorInfo
            : localError(
                "E_CLIENT",
                error instanceof Error ? error.message : String(error),
              ),
      };
    }
  }

  resume(): void {
    store.dispatch({ type: "intent/resume" });
  }
}

export const controller = new GatewayController();
