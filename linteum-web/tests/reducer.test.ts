import { describe, expect, it } from "vitest";
import {
  DEFAULT_LEFT_RAIL_WIDTH,
  DEFAULT_RIGHT_RAIL_WIDTH,
  MAX_LEFT_RAIL_WIDTH,
  STORAGE_KEYS,
} from "../src/config";
import type { PluginSummary } from "../src/protocol/commands";
import type { ViewFrame } from "../src/protocol/frames";
import { reduce } from "../src/state/reducer";
import { INITIAL_CONFIGURATION } from "../src/state/configuration";
import type { ActiveSessionState, AppState } from "../src/state/types";

function activeSession(): ActiveSessionState {
  return {
    id: "sess-1",
    info: null,
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
    hydrating: false,
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

const demoPlugin: PluginSummary = {
  id: "demo",
  name: "Demo plugin",
  version: "1.0.0",
  state: "disabled",
  enabled: false,
  description: "A demo plugin.",
  tools: [],
  skills: [],
  modes: [],
  warnings: [],
  hooks: [],
  subscribes: [],
  publishes: [],
  config: {},
  secrets: [],
};

function state(): AppState {
  return {
    connection: {
      status: "ready",
      url: "ws://127.0.0.1:8765/",
      instanceId: "instance-1",
      protocol: 1,
      features: [],
      limits: {},
      error: null,
      retryAt: null,
    },
    catalog: { items: [], loading: false, error: null },
    active: activeSession(),
    plugins: {
      items: [],
      loading: false,
      manage: false,
      allow: null,
      applying: false,
      lastApply: null,
      error: null,
      pendingId: null,
      lastAction: null,
    },
    configuration: INITIAL_CONFIGURATION,
    ui: {
      page: "chat",
      theme: "dark",
      locale: "en",
      leftRailOpen: true,
      rightRailOpen: true,
      leftRailWidth: DEFAULT_LEFT_RAIL_WIDTH,
      rightRailWidth: DEFAULT_RIGHT_RAIL_WIDTH,
      filePaneOpen: false,
      filePaneWidth: 460,
      filePanePosition: "outer",
      openFiles: [],
      activeFilePath: null,
      panelMode: "stack",
      openPanels: {},
      activePanelId: null,
      overlays: [],
      composer: "",
      activityOpen: false,
      activity: [],
      artifacts: {},
    },
  };
}

function userFrame(seq: number, text: string): ViewFrame {
  return {
    kind: "user.message",
    wire: "user.message",
    seq,
    ordinal: 0,
    replay: false,
    ephemeral: false,
    text,
  };
}

describe("reducer", () => {
  it("appends a user message and advances the cursor", () => {
    const result = reduce(state(), { type: "gateway/frame", frame: userFrame(1, "hello") });
    expect(result.state.active?.transcript).toHaveLength(1);
    const item = result.state.active?.transcript[0];
    expect(item?.kind).toBe("user");
    if (item?.kind === "user") expect(item.text).toBe("hello");
    expect(result.state.active?.lastSeq).toBe(1);
  });

  it("ignores frames belonging to another session", () => {
    const leak: ViewFrame = { ...userFrame(1, "leak"), sessionId: "sess-other" };
    const result = reduce(state(), { type: "gateway/frame", frame: leak });
    expect(result.state.active?.transcript).toHaveLength(0);
  });

  it("unsubscribes the previous session when switching", () => {
    const result = reduce(
      state(),
      { type: "intent/openSession", sessionId: "sess-2" },
    );
    expect(result.state.active?.id).toBe("sess-2");
    expect(
      result.effects.some(
        (effect) =>
          effect.type === "command" &&
          effect.name === "session.unsubscribe" &&
          effect.sessionId === "sess-1",
      ),
    ).toBe(true);
  });

  it("deduplicates durable frames by seq/frame/ordinal", () => {
    let current = state();
    current = reduce(current, { type: "gateway/frame", frame: userFrame(1, "hello") }).state;
    current = reduce(current, { type: "gateway/frame", frame: userFrame(1, "hello") }).state;
    expect(current.active?.transcript).toHaveLength(1);
  });

  it("does not deduplicate ephemeral deltas", () => {
    const delta: ViewFrame = {
      kind: "assistant.delta",
      wire: "assistant.delta",
      seq: 0,
      ordinal: 0,
      replay: false,
      ephemeral: true,
      text: "hello",
    };
    let current = state();
    current = reduce(current, { type: "gateway/frame", frame: delta }).state;
    current = reduce(current, { type: "gateway/frame", frame: delta }).state;
    const item = current.active?.transcript[0];
    expect(item?.kind).toBe("assistant");
    if (item?.kind === "assistant") expect(item.text).toBe("hellohello");
  });

  it("resubscribes after a history gap", () => {
    const gap: ViewFrame = {
      kind: "gap",
      wire: "gap",
      seq: 5,
      ordinal: 0,
      replay: false,
      ephemeral: false,
      fromSeq: 2,
      toSeq: 5,
    };
    const result = reduce(state(), { type: "gateway/frame", frame: gap });
    expect(result.state.active?.lastSeq).toBe(5);
    expect(
      result.effects.some(
        (effect) => effect.type === "command" && effect.name === "session.subscribe",
      ),
    ).toBe(true);
    expect(result.state.ui.activity.some((entry) => entry.kind === "gap")).toBe(true);
  });

  it("accumulates live usage but ignores replayed usage", () => {
    const live: ViewFrame = {
      kind: "assistant.message",
      wire: "assistant.message",
      seq: 1,
      ordinal: 0,
      replay: false,
      ephemeral: false,
      text: "hi",
      toolCalls: [],
      usage: { input_tokens: 3, output_tokens: 2 },
      cost: {},
      interrupted: false,
    };
    const replayed: ViewFrame = { ...live, seq: 2, replay: true };
    let current = reduce(state(), { type: "gateway/frame", frame: live }).state;
    current = reduce(current, { type: "gateway/frame", frame: replayed }).state;
    expect(current.active?.usage.input_tokens).toBe(3);
    expect(current.active?.usage.output_tokens).toBe(2);
  });

  it("merges tool deltas and results by call id", () => {
    const requested: ViewFrame = {
      kind: "tool.requested",
      wire: "tool.requested",
      seq: 1,
      ordinal: 0,
      replay: false,
      ephemeral: false,
      callId: "call-1",
      name: "bash",
      args: { command: "ls" },
    };
    const delta: ViewFrame = {
      kind: "tool.delta",
      wire: "tool.delta",
      seq: 0,
      ordinal: 0,
      replay: false,
      ephemeral: true,
      callId: "call-1",
      text: "partial",
      background: false,
    };
    const result: ViewFrame = {
      kind: "tool.result",
      wire: "tool.result",
      seq: 2,
      ordinal: 0,
      replay: false,
      ephemeral: false,
      callId: "call-1",
      name: "bash",
      ok: true,
      output: "done",
      outputRef: null,
      dedup: false,
      code: null,
      error: null,
      errorInfo: null,
      background: false,
      taskId: null,
      adopted: false,
    };
    let current = reduce(state(), { type: "gateway/frame", frame: requested }).state;
    current = reduce(current, { type: "gateway/frame", frame: delta }).state;
    current = reduce(current, { type: "gateway/frame", frame: result }).state;
    expect(current.active?.transcript).toHaveLength(1);
    const item = current.active?.transcript[0];
    expect(item?.kind).toBe("tool");
    if (item?.kind === "tool") {
      expect(item.callId).toBe("call-1");
      expect(item.status).toBe("ok");
      expect(item.output).toBe("done");
    }
  });

  it("applies a snapshot baseline without clearing the transcript", () => {
    let current = reduce(state(), { type: "gateway/frame", frame: userFrame(1, "hi") }).state;
    const snapshot: ViewFrame = {
      kind: "snapshot",
      wire: "snapshot",
      seq: 4,
      ordinal: 0,
      replay: true,
      ephemeral: false,
      sessionId: "sess-1",
      snapshot: {
        lastSeq: 4,
        phase: "thinking",
        round: 2,
        busy: true,
        pending: [],
        streaming: null,
        approvals: [],
        usage: { input_tokens: 7 },
        boundModel: "openai/gpt-test",
        mode: null,
        contextDigest: "digest",
        lastError: null,
        background: [],
        backgroundRunning: 0,
        backgroundTaskIds: [],
      },
    };
    current = reduce(current, { type: "gateway/frame", frame: snapshot }).state;
    expect(current.active?.transcript).toHaveLength(1);
    expect(current.active?.lastSeq).toBe(4);
    expect(current.active?.busy).toBe(true);
    expect(current.active?.usage.input_tokens).toBe(7);
  });

  it("opens a session in the hydrating state", () => {
    const result = reduce(state(), {
      type: "intent/openSession",
      sessionId: "sess-2",
    });
    expect(result.state.active?.hydrating).toBe(true);
  });

  it("applies all replayed frames in one transition when hydration completes", () => {
    const opened = reduce(state(), {
      type: "intent/openSession",
      sessionId: "sess-2",
    }).state;
    const result = reduce(opened, {
      type: "session/subscribed",
      sessionId: "sess-2",
      snapshotSeq: 2,
      frames: [userFrame(1, "hello"), userFrame(2, "world")],
    });
    expect(result.state.active?.transcript).toHaveLength(2);
    expect(result.state.active?.hydrating).toBe(false);
    expect(result.state.active?.lastSeq).toBe(2);
  });

  it("does not leave replayed approvals behind after hydration", () => {
    const required: ViewFrame = {
      kind: "approval.required",
      wire: "approval.required",
      seq: 1,
      ordinal: 0,
      replay: true,
      ephemeral: false,
      approval: { callId: "call-1", name: "bash" },
    };
    const resolved: ViewFrame = {
      kind: "approval.resolved",
      wire: "approval.resolved",
      seq: 2,
      ordinal: 0,
      replay: true,
      ephemeral: false,
      callId: "call-1",
      decision: "granted",
    };
    const opened = reduce(state(), {
      type: "intent/openSession",
      sessionId: "sess-2",
    }).state;
    const result = reduce(opened, {
      type: "session/subscribed",
      sessionId: "sess-2",
      snapshotSeq: 2,
      frames: [required, resolved],
    });
    expect(result.state.active?.approvals).toHaveLength(0);
    expect(result.state.active?.hydrating).toBe(false);
  });

  it("ignores replayed approval transitions; the snapshot owns pending approvals", () => {
    const required: ViewFrame = {
      kind: "approval.required",
      wire: "approval.required",
      seq: 1,
      ordinal: 0,
      replay: true,
      ephemeral: false,
      approval: { callId: "call-1", name: "bash" },
    };
    const result = reduce(state(), { type: "gateway/frame", frame: required });
    expect(result.state.active?.approvals).toHaveLength(0);
  });

  it("applies live approval requests", () => {
    const required: ViewFrame = {
      kind: "approval.required",
      wire: "approval.required",
      seq: 1,
      ordinal: 0,
      replay: false,
      ephemeral: false,
      approval: { callId: "call-1", name: "bash" },
    };
    const result = reduce(state(), { type: "gateway/frame", frame: required });
    expect(result.state.active?.approvals).toHaveLength(1);
  });

  it("keeps a genuinely pending approval from the snapshot", () => {
    const snapshot: ViewFrame = {
      kind: "snapshot",
      wire: "snapshot",
      seq: 2,
      ordinal: 0,
      replay: true,
      ephemeral: false,
      sessionId: "sess-2",
      snapshot: {
        lastSeq: 2,
        phase: "awaiting_approval",
        round: 1,
        busy: true,
        pending: ["call-1"],
        streaming: null,
        approvals: [{ callId: "call-1", name: "bash" }],
        usage: {},
        boundModel: null,
        mode: null,
        contextDigest: null,
        lastError: null,
        background: [],
        backgroundRunning: 0,
        backgroundTaskIds: [],
      },
    };
    const opened = reduce(state(), {
      type: "intent/openSession",
      sessionId: "sess-2",
    }).state;
    const outcome = reduce(opened, {
      type: "session/subscribed",
      sessionId: "sess-2",
      snapshotSeq: 2,
      frames: [snapshot],
    });
    expect(outcome.state.active?.approvals).toHaveLength(1);
    expect(outcome.state.active?.hydrating).toBe(false);
  });

  it("keeps hydrating when the replay ends in a gap and resubscribes", () => {
    const gap: ViewFrame = {
      kind: "gap",
      wire: "gap",
      seq: 5,
      ordinal: 0,
      replay: false,
      ephemeral: false,
      fromSeq: 2,
      toSeq: 5,
    };
    const opened = reduce(state(), {
      type: "intent/openSession",
      sessionId: "sess-2",
    }).state;
    const result = reduce(opened, {
      type: "session/subscribed",
      sessionId: "sess-2",
      snapshotSeq: 5,
      frames: [gap],
    });
    expect(result.state.active?.hydrating).toBe(true);
    expect(
      result.effects.some(
        (effect) => effect.type === "command" && effect.name === "session.subscribe",
      ),
    ).toBe(true);
  });

  it("ends hydration when subscribe fails for a non-connection reason", () => {
    const opened = reduce(state(), {
      type: "intent/openSession",
      sessionId: "sess-2",
    }).state;
    const result = reduce(opened, {
      type: "gateway/commandFailed",
      name: "session.subscribe",
      error: { code: "E_PROTOCOL", message: "subscribe rejected" },
      sessionId: "sess-2",
    });
    expect(result.state.active?.hydrating).toBe(false);
  });

  it("fills a restored session's workspace from the catalog", () => {
    const restored = reduce(state(), {
      type: "intent/openSession",
      sessionId: "sess-2",
    }).state;
    const result = reduce(restored, {
      type: "catalog/loaded",
      sessions: [
        {
          session_id: "sess-2",
          created_at: 0,
          updated_at: 0,
          phase: "idle",
          round: 0,
          message_count: 0,
          last_seq: 0,
          workspace: "/workspace/project",
        },
      ],
      limit: 50,
      offset: 0,
      append: false,
    });
    expect(result.state.active?.info?.workspace).toBe("/workspace/project");
  });

  it("clears the tab-scoped session when subscribe reports it gone", () => {
    const opened = reduce(state(), {
      type: "intent/openSession",
      sessionId: "sess-2",
    }).state;
    const result = reduce(opened, {
      type: "gateway/commandFailed",
      name: "session.subscribe",
      error: { code: "E_SESSION_NOT_FOUND", message: "session is gone" },
      sessionId: "sess-2",
    });
    expect(result.state.active).toBeNull();
    expect(result.effects).toContainEqual({
      type: "persist",
      key: STORAGE_KEYS.lastSession,
      value: null,
    });
  });

  it("keeps hydration pending when the connection drops", () => {
    const opened = reduce(state(), {
      type: "intent/openSession",
      sessionId: "sess-2",
    }).state;
    const result = reduce(opened, {
      type: "gateway/commandFailed",
      name: "session.subscribe",
      error: { code: "E_CONNECTION_CLOSED", message: "gateway reconnecting" },
      sessionId: "sess-2",
    });
    expect(result.state.active?.hydrating).toBe(true);
  });

  it("clears a pending approval when its live tool result arrives", () => {
    const required: ViewFrame = {
      kind: "approval.required",
      wire: "approval.required",
      seq: 1,
      ordinal: 0,
      replay: false,
      ephemeral: false,
      approval: { callId: "call-1", name: "bash" },
    };
    const result: ViewFrame = {
      kind: "tool.result",
      wire: "tool.result",
      seq: 2,
      ordinal: 0,
      replay: false,
      ephemeral: false,
      callId: "call-1",
      name: "bash",
      ok: false,
      output: null,
      outputRef: null,
      dedup: false,
      code: "E_INTERRUPTED",
      error: "interrupted",
      errorInfo: { code: "E_INTERRUPTED", message: "interrupted" },
      background: false,
      taskId: null,
      adopted: false,
    };
    const opened = reduce(state(), {
      type: "intent/openSession",
      sessionId: "sess-2",
    }).state;
    const outcome = reduce(opened, {
      type: "session/subscribed",
      sessionId: "sess-2",
      snapshotSeq: 2,
      frames: [required, result],
    });
    expect(outcome.state.active?.approvals).toHaveLength(0);
  });

  it("clears a pending approval when a live interrupt notice arrives", () => {
    const required: ViewFrame = {
      kind: "approval.required",
      wire: "approval.required",
      seq: 1,
      ordinal: 0,
      replay: false,
      ephemeral: false,
      approval: { callId: "call-1", name: "bash" },
    };
    const interrupt: ViewFrame = {
      kind: "notice",
      wire: "notice",
      seq: 2,
      ordinal: 0,
      replay: false,
      ephemeral: false,
      noticeKind: "interrupt",
      payload: { kind: "interrupt", call_id: "call-1", scope: "foreground" },
    };
    const opened = reduce(state(), {
      type: "intent/openSession",
      sessionId: "sess-2",
    }).state;
    const outcome = reduce(opened, {
      type: "session/subscribed",
      sessionId: "sess-2",
      snapshotSeq: 2,
      frames: [required, interrupt],
    });
    expect(outcome.state.active?.approvals).toHaveLength(0);
  });

  it("switches between the chat and settings pages", () => {
    const settings = reduce(state(), { type: "ui/page", page: "settings" }).state;
    expect(settings.ui.page).toBe("settings");
    const chat = reduce(settings, { type: "ui/page", page: "chat" }).state;
    expect(chat.ui.page).toBe("chat");
  });

  it("clamps a resized rail and persists the committed width", () => {
    const result = reduce(state(), {
      type: "ui/railWidth",
      side: "left",
      width: 9_999,
      commit: true,
    });
    expect(result.state.ui.leftRailWidth).toBe(MAX_LEFT_RAIL_WIDTH);
    expect(result.effects).toContainEqual({
      type: "persist",
      key: STORAGE_KEYS.leftRailWidth,
      value: String(MAX_LEFT_RAIL_WIDTH),
    });
  });

  it("previews a rail width without persisting it during a drag", () => {
    const result = reduce(state(), {
      type: "ui/railWidth",
      side: "right",
      width: 400,
    });
    expect(result.state.ui.rightRailWidth).toBe(400);
    expect(result.effects).toHaveLength(0);
  });

  it("switches the locale and persists it", () => {
    const result = reduce(state(), { type: "ui/locale", locale: "zh-CN" });
    expect(result.state.ui.locale).toBe("zh-CN");
    expect(result.effects).toContainEqual({
      type: "persist",
      key: STORAGE_KEYS.locale,
      value: "zh-CN",
    });
  });

  it("loads the plugin catalogue without an active session", () => {
    const withoutActive = { ...state(), active: null };
    const loading = reduce(withoutActive, { type: "intent/refreshPlugins" });
    expect(loading.state.plugins.loading).toBe(true);
    expect(loading.effects).toContainEqual({
      type: "command",
      name: "plugin.list",
      args: {},
    });

    const loaded = reduce(loading.state, {
      type: "plugins/loaded",
      plugins: [demoPlugin],
      manage: true,
      allow: ["demo"],
    });
    expect(loaded.state.plugins.items).toHaveLength(1);
    expect(loaded.state.plugins.manage).toBe(true);
    expect(loaded.state.plugins.allow).toEqual(["demo"]);
    expect(loaded.state.plugins.loading).toBe(false);
  });

  it("requests plugin enable/disable only when management is allowed", () => {
    const allowed = {
      ...state(),
      connection: { ...state().connection, features: ["plugins.manage"] },
      plugins: { ...state().plugins, items: [demoPlugin], manage: true },
    };
    const disable = reduce(allowed, {
      type: "intent/pluginSetEnabled",
      id: "demo",
      enabled: false,
    });
    expect(disable.state.plugins.pendingId).toBe("demo");
    expect(disable.effects).toContainEqual({
      type: "command",
      name: "plugin.disable",
      args: { id: "demo" },
    });

    const denied = { ...allowed, plugins: { ...allowed.plugins, manage: false } };
    const refused = reduce(denied, {
      type: "intent/pluginSetEnabled",
      id: "demo",
      enabled: true,
    });
    expect(refused.effects).toHaveLength(0);
    expect(refused.state.plugins.pendingId).toBeNull();

    const offline = {
      ...allowed,
      connection: { ...allowed.connection, status: "closed" as const },
    };
    const skipped = reduce(offline, {
      type: "intent/pluginSetEnabled",
      id: "demo",
      enabled: true,
    });
    expect(skipped.effects).toHaveLength(0);
  });

  it("records a managed plugin and its restart requirement", () => {
    const allowed = {
      ...state(),
      connection: { ...state().connection, features: ["plugins.manage"] },
      plugins: { ...state().plugins, items: [demoPlugin], manage: true },
    };
    const managing = reduce(allowed, {
      type: "intent/pluginSetEnabled",
      id: "demo",
      enabled: true,
    }).state;
    const result = reduce(managing, {
      type: "plugins/managed",
      plugin: { ...demoPlugin, enabled: true },
      changed: true,
      restartRequired: true,
    });
    expect(result.state.plugins.pendingId).toBeNull();
    expect(result.state.plugins.items[0]?.enabled).toBe(true);
    expect(result.state.plugins.lastAction).toEqual({
      id: "demo",
      enabled: true,
      changed: true,
      restartRequired: true,
    });
  });

  it("auto-applies the updated allow list after a manage command", () => {
    const allowed = {
      ...state(),
      connection: {
        ...state().connection,
        features: ["plugins.manage", "plugins.runtime"],
      },
      plugins: {
        ...state().plugins,
        items: [demoPlugin],
        manage: true,
        allow: [],
      },
    };
    const result = reduce(allowed, {
      type: "plugins/managed",
      plugin: { ...demoPlugin, enabled: true },
      changed: true,
      restartRequired: true,
    });
    expect(result.state.plugins.allow).toEqual(["demo"]);
    expect(result.state.plugins.applying).toBe(true);
    expect(result.effects).toContainEqual({
      type: "command",
      name: "plugin.set_allowed",
      args: { ids: ["demo"], mode: "apply" },
    });
  });

  it("removes a disabled plugin from allow before applying", () => {
    const allowed = {
      ...state(),
      connection: {
        ...state().connection,
        features: ["plugins.manage", "plugins.runtime"],
      },
      plugins: {
        ...state().plugins,
        items: [{ ...demoPlugin, enabled: true, state: "active" }],
        manage: true,
        allow: ["demo", "other"],
      },
    };
    const result = reduce(allowed, {
      type: "plugins/managed",
      plugin: { ...demoPlugin, enabled: false, state: "active" },
      changed: true,
      restartRequired: true,
    });
    expect(result.state.plugins.allow).toEqual(["other"]);
    expect(result.effects).toContainEqual({
      type: "command",
      name: "plugin.set_allowed",
      args: { ids: ["other"], mode: "apply" },
    });
  });

  it("still mirrors allow when runtime apply is unavailable", () => {
    const allowed = {
      ...state(),
      connection: { ...state().connection, features: ["plugins.manage"] },
      plugins: {
        ...state().plugins,
        items: [demoPlugin],
        manage: true,
        allow: [],
      },
    };
    const result = reduce(allowed, {
      type: "plugins/managed",
      plugin: { ...demoPlugin, enabled: true },
      changed: true,
      restartRequired: true,
    });
    expect(result.state.plugins.allow).toEqual(["demo"]);
    expect(result.state.plugins.applying).toBe(false);
    expect(result.effects).toHaveLength(0);
  });

  it("does not apply without a known allow list", () => {
    const allowed = {
      ...state(),
      connection: {
        ...state().connection,
        features: ["plugins.manage", "plugins.runtime"],
      },
      plugins: {
        ...state().plugins,
        items: [demoPlugin],
        manage: true,
        allow: null,
      },
    };
    const result = reduce(allowed, {
      type: "plugins/managed",
      plugin: { ...demoPlugin, enabled: true },
      changed: true,
      restartRequired: true,
    });
    expect(result.state.plugins.allow).toBeNull();
    expect(result.state.plugins.applying).toBe(false);
    expect(result.effects).toHaveLength(0);
  });

  it("applies an empty allow list when the last plugin is disabled", () => {
    const allowed = {
      ...state(),
      connection: {
        ...state().connection,
        features: ["plugins.manage", "plugins.runtime"],
      },
      plugins: {
        ...state().plugins,
        items: [{ ...demoPlugin, enabled: true, state: "active" }],
        manage: true,
        allow: ["demo"],
      },
    };
    const result = reduce(allowed, {
      type: "plugins/managed",
      plugin: { ...demoPlugin, enabled: false, state: "active" },
      changed: true,
      restartRequired: true,
    });
    expect(result.state.plugins.allow).toEqual([]);
    expect(result.effects).toContainEqual({
      type: "command",
      name: "plugin.set_allowed",
      args: { ids: [], mode: "apply" },
    });
  });

  it("applies the saved allow list on demand", () => {
    const ready = {
      ...state(),
      connection: {
        ...state().connection,
        features: ["plugins.manage", "plugins.runtime"],
      },
      plugins: {
        ...state().plugins,
        items: [{ ...demoPlugin, enabled: true, state: "disabled" }],
        manage: true,
        allow: ["demo"],
      },
    };
    const result = reduce(ready, { type: "intent/applyPlugins" });
    expect(result.state.plugins.applying).toBe(true);
    expect(result.effects).toContainEqual({
      type: "command",
      name: "plugin.set_allowed",
      args: { ids: ["demo"], mode: "apply" },
    });
  });

  it("refuses on-demand apply without the runtime feature", () => {
    const legacy = {
      ...state(),
      connection: { ...state().connection, features: ["plugins.manage"] },
      plugins: { ...state().plugins, manage: true, allow: ["demo"] },
    };
    const result = reduce(legacy, { type: "intent/applyPlugins" });
    expect(result.state.plugins.applying).toBe(false);
    expect(result.effects).toHaveLength(0);
  });

  it("skips apply when the manage command changed nothing", () => {
    const allowed = {
      ...state(),
      connection: {
        ...state().connection,
        features: ["plugins.manage", "plugins.runtime"],
      },
      plugins: {
        ...state().plugins,
        items: [{ ...demoPlugin, enabled: true }],
        manage: true,
        allow: ["demo"],
      },
    };
    const result = reduce(allowed, {
      type: "plugins/managed",
      plugin: { ...demoPlugin, enabled: true },
      changed: false,
      restartRequired: false,
    });
    expect(result.state.plugins.applying).toBe(false);
    expect(result.effects).toHaveLength(0);
  });

  it("records the runtime apply result", () => {
    const applying = {
      ...state(),
      plugins: {
        ...state().plugins,
        items: [demoPlugin],
        manage: true,
        allow: ["demo"],
        applying: true,
      },
    };
    const change = { id: "demo", action: "load", state: "active" };
    const result = reduce(applying, {
      type: "plugins/applied",
      allow: ["demo"],
      plugins: [{ ...demoPlugin, enabled: true, state: "active" }],
      restartRequired: false,
      changes: [change],
    });
    expect(result.state.plugins.applying).toBe(false);
    expect(result.state.plugins.lastApply).toEqual({
      restartRequired: false,
      changes: [change],
    });
    expect(result.state.plugins.items[0]?.state).toBe("active");
  });

  it("keeps plugins that dropped out of allow after applying", () => {
    const other: PluginSummary = {
      ...demoPlugin,
      id: "other",
      name: "Other plugin",
      enabled: true,
      state: "active",
    };
    const applying = {
      ...state(),
      plugins: {
        ...state().plugins,
        items: [{ ...demoPlugin, enabled: true, state: "active" }, other],
        manage: true,
        allow: ["demo", "other"],
        applying: true,
      },
    };
    const result = reduce(applying, {
      type: "plugins/applied",
      allow: ["other"],
      // `set_allowed` summaries only cover ids that remain in allow.
      plugins: [other],
      restartRequired: false,
      changes: [{ id: "demo", action: "unload", state: "unloaded" }],
    });
    expect(result.state.plugins.items).toHaveLength(2);
    const demo = result.state.plugins.items.find((plugin) => plugin.id === "demo");
    expect(demo?.enabled).toBe(false);
    expect(demo?.state).toBe("unloaded");
    expect(result.state.plugins.items.find((plugin) => plugin.id === "other")?.enabled).toBe(
      true,
    );
  });

  it("restores file-level enablement from allow after applying", () => {
    const applying = {
      ...state(),
      plugins: {
        ...state().plugins,
        items: [demoPlugin],
        manage: true,
        allow: ["demo"],
        applying: true,
      },
    };
    const result = reduce(applying, {
      type: "plugins/applied",
      allow: ["demo"],
      // `plugin.set_allowed` summaries do not carry `enabled`.
      plugins: [{ ...demoPlugin, enabled: undefined, state: "active" }],
      restartRequired: false,
      changes: [],
    });
    expect(result.state.plugins.items[0]?.enabled).toBe(true);
  });

  it("clears the apply flag when set_allowed fails", () => {
    const applying = {
      ...state(),
      plugins: { ...state().plugins, applying: true },
    };
    const result = reduce(applying, {
      type: "gateway/commandFailed",
      name: "plugin.set_allowed",
      error: { code: "E_PLUGIN_MANAGE_DISABLED", message: "remote transport" },
    });
    expect(result.state.plugins.applying).toBe(false);
    expect(result.state.plugins.error?.code).toBe("E_PLUGIN_MANAGE_DISABLED");
  });

  it("clears plugin pending state when the command fails", () => {
    const allowed = {
      ...state(),
      connection: { ...state().connection, features: ["plugins.manage"] },
      plugins: { ...state().plugins, items: [demoPlugin], manage: true },
    };
    const managing = reduce(allowed, {
      type: "intent/pluginSetEnabled",
      id: "demo",
      enabled: true,
    }).state;
    const result = reduce(managing, {
      type: "gateway/commandFailed",
      name: "plugin.enable",
      error: { code: "E_PLUGIN_MANAGE_DISABLED", message: "plugin management is disabled" },
    });
    expect(result.state.plugins.pendingId).toBeNull();
    expect(result.state.plugins.error?.code).toBe("E_PLUGIN_MANAGE_DISABLED");
  });

  it("keeps plugin metadata when a manage result omits it", () => {
    const allowed = {
      ...state(),
      connection: { ...state().connection, features: ["plugins.manage"] },
      plugins: {
        ...state().plugins,
        items: [{ ...demoPlugin, description: "Keep me" }],
        manage: true,
        pendingId: "demo",
      },
    };
    const result = reduce(allowed, {
      type: "plugins/managed",
      // `parsePluginSummary` always returns every key; absent fields are
      // `undefined` rather than omitted.
      plugin: {
        id: "demo",
        name: undefined,
        version: undefined,
        state: undefined,
        enabled: true,
        description: undefined,
        trust: undefined,
        source: undefined,
        tools: [],
        skills: [],
        modes: [],
        digest: undefined,
        warnings: [],
        hooks: [],
        subscribes: [],
        publishes: [],
        config: {},
        secrets: [],
        error: undefined,
      },
      changed: true,
      restartRequired: false,
    });
    expect(result.state.plugins.items[0]?.description).toBe("Keep me");
  });

  it("sends the mode name to session.mode.set", () => {
    const result = reduce(state(), { type: "intent/modeSet", modeName: "code" });
    expect(result.effects[0]).toMatchObject({
      type: "command",
      name: "session.mode.set",
      args: { mode: "code" },
      sessionId: "sess-1",
    });
  });

  it("drops the cached context when the mode binding changes", () => {
    const base = state();
    const withContext = {
      ...base,
      active: {
        ...base.active!,
        mode: { id: "builtin/standard", name: "standard", digest: "sha256:std" },
        context: {
          session_id: "sess-1",
          kind: "root",
          phase: "idle",
          round: 0,
          source: "live",
          system_blocks_included: true,
          digest: "sha256:old",
          estimated_tokens: 10,
          messages: [],
          messages_total: 0,
          offset: 0,
          next_offset: 0,
          has_more: false,
          truncated: false,
          tools: ["read_file"],
        },
      },
    };
    const result = reduce(withContext, {
      type: "gateway/frame",
      frame: {
        kind: "mode",
        wire: "mode",
        seq: 2,
        ordinal: 0,
        replay: false,
        ephemeral: false,
        mode: { id: "builtin/code", name: "code", digest: "sha256:new" },
      },
    });
    expect(result.state.active?.mode?.id).toBe("builtin/code");
    // The cached plan still lists the old mode's tools.
    expect(result.state.active?.context).toBeNull();
  });

  it("keeps the cached context when a mode frame repeats the binding", () => {
    const base = state();
    const context = {
      session_id: "sess-1",
      kind: "root",
      phase: "idle",
      round: 0,
      source: "live",
      system_blocks_included: true,
      digest: "sha256:ctx",
      estimated_tokens: 10,
      messages: [],
      messages_total: 0,
      offset: 0,
      next_offset: 0,
      has_more: false,
      truncated: false,
      tools: ["read_file"],
    };
    const withContext = {
      ...base,
      active: {
        ...base.active!,
        mode: { id: "builtin/code", name: "code", digest: "sha256:code" },
        context,
      },
    };
    const result = reduce(withContext, {
      type: "gateway/frame",
      frame: {
        kind: "mode",
        wire: "mode",
        seq: 2,
        ordinal: 0,
        replay: false,
        ephemeral: false,
        mode: { id: "builtin/code", name: "code", digest: "sha256:code" },
      },
    });
    expect(result.state.active?.context).toBe(context);
  });

  it("drops the cached context when a snapshot reports a new mode", () => {
    const base = state();
    const withContext = {
      ...base,
      active: {
        ...base.active!,
        mode: { id: "builtin/standard", name: "standard", digest: "sha256:std" },
        context: {
          session_id: "sess-1",
          kind: "root",
          phase: "idle",
          round: 0,
          source: "live",
          system_blocks_included: true,
          digest: "sha256:old",
          estimated_tokens: 10,
          messages: [],
          messages_total: 0,
          offset: 0,
          next_offset: 0,
          has_more: false,
          truncated: false,
          tools: ["read_file"],
        },
      },
    };
    const result = reduce(withContext, {
      type: "gateway/frame",
      frame: {
        kind: "snapshot",
        wire: "snapshot",
        seq: 3,
        ordinal: 0,
        replay: false,
        ephemeral: false,
        sessionId: "sess-1",
        snapshot: {
          lastSeq: 3,
          phase: "idle",
          round: 0,
          busy: false,
          pending: [],
          streaming: null,
          approvals: [],
          usage: {},
          boundModel: null,
          mode: { id: "builtin/code", name: "code", digest: "sha256:code" },
          contextDigest: null,
          lastError: null,
          background: [],
          backgroundRunning: 0,
          backgroundTaskIds: [],
        },
      },
    });
    expect(result.state.active?.mode?.id).toBe("builtin/code");
    expect(result.state.active?.context).toBeNull();
  });

  it("keeps the cached context when a snapshot repeats the mode binding", () => {
    const base = state();
    const context = {
      session_id: "sess-1",
      kind: "root",
      phase: "idle",
      round: 0,
      source: "live",
      system_blocks_included: true,
      digest: "sha256:ctx",
      estimated_tokens: 10,
      messages: [],
      messages_total: 0,
      offset: 0,
      next_offset: 0,
      has_more: false,
      truncated: false,
      tools: ["read_file"],
    };
    const withContext = {
      ...base,
      active: {
        ...base.active!,
        mode: { id: "builtin/code", name: "code", digest: "sha256:code" },
        context,
      },
    };
    const result = reduce(withContext, {
      type: "gateway/frame",
      frame: {
        kind: "snapshot",
        wire: "snapshot",
        seq: 3,
        ordinal: 0,
        replay: false,
        ephemeral: false,
        sessionId: "sess-1",
        snapshot: {
          lastSeq: 3,
          phase: "idle",
          round: 0,
          busy: false,
          pending: [],
          streaming: null,
          approvals: [],
          usage: {},
          boundModel: null,
          mode: { id: "builtin/code", name: "code", digest: "sha256:code" },
          contextDigest: null,
          lastError: null,
          background: [],
          backgroundRunning: 0,
          backgroundTaskIds: [],
        },
      },
    });
    expect(result.state.active?.context).toBe(context);
  });

  it("opens a file in the file sidebar and requests its content", () => {
    const result = reduce(state(), {
      type: "file/open",
      root: "/w",
      path: "/w/a.py",
      name: "a.py",
    });
    expect(result.state.ui.openFiles).toHaveLength(1);
    expect(result.state.ui.openFiles[0]).toMatchObject({
      path: "/w/a.py",
      root: "/w",
      name: "a.py",
      loading: true,
    });
    expect(result.state.ui.activeFilePath).toBe("/w/a.py");
    expect(result.state.ui.filePaneOpen).toBe(true);
    expect(result.effects).toEqual([
      { type: "workspaceRead", root: "/w", path: "/w/a.py" },
    ]);
  });

  it("focuses an already-open file without reading it again", () => {
    const opened = reduce(state(), {
      type: "file/open",
      root: "/w",
      path: "/w/a.py",
      name: "a.py",
    });
    const loaded = reduce(opened.state, {
      type: "file/loaded",
      root: "/w",
      path: "/w/a.py",
      result: { text: "print('hi')", size: 11 },
    });
    const again = reduce(loaded.state, {
      type: "file/open",
      root: "/w",
      path: "/w/a.py",
      name: "a.py",
    });
    expect(again.state.ui.openFiles).toHaveLength(1);
    expect(again.effects).toEqual([]);
  });

  it("closes the active tab onto a neighbour and hides an empty pane", () => {
    let current = reduce(state(), {
      type: "file/open",
      root: "/w",
      path: "/w/a.py",
      name: "a.py",
    }).state;
    current = reduce(current, {
      type: "file/open",
      root: "/w",
      path: "/w/b.rs",
      name: "b.rs",
    }).state;
    current = reduce(current, {
      type: "file/close",
      path: "/w/b.rs",
    }).state;
    expect(current.ui.activeFilePath).toBe("/w/a.py");
    expect(current.ui.openFiles).toHaveLength(1);
    expect(current.ui.filePaneOpen).toBe(true);
    current = reduce(current, { type: "file/close", path: "/w/a.py" }).state;
    expect(current.ui.openFiles).toEqual([]);
    expect(current.ui.activeFilePath).toBeNull();
    expect(current.ui.filePaneOpen).toBe(false);
  });

  it("clears the file preview when switching sessions", () => {
    let current = reduce(state(), {
      type: "file/open",
      root: "/w",
      path: "/w/a.py",
      name: "a.py",
    }).state;
    current = reduce(current, {
      type: "file/loaded",
      root: "/w",
      path: "/w/a.py",
      result: { text: "print('hi')" },
    }).state;
    const switched = reduce(current, {
      type: "intent/openSession",
      sessionId: "sess-2",
      info: {
        session_id: "sess-2",
        created_at: 0,
        updated_at: 0,
        phase: "idle",
        round: 0,
        message_count: 0,
        last_seq: 0,
        workspace: "/other",
      },
    });
    expect(switched.state.active?.id).toBe("sess-2");
    // The previous workspace's file must not leak into the new session.
    expect(switched.state.ui.openFiles).toEqual([]);
    expect(switched.state.ui.activeFilePath).toBeNull();
    expect(switched.state.ui.filePaneOpen).toBe(false);
  });

  it("clears the file preview when creating or leaving a session", () => {
    const opened = reduce(state(), {
      type: "file/open",
      root: "/w",
      path: "/w/a.py",
      name: "a.py",
    }).state;
    const created = reduce(opened, {
      type: "session/created",
      info: {
        session_id: "sess-new",
        created_at: 0,
        updated_at: 0,
        phase: "idle",
        round: 0,
        message_count: 0,
        last_seq: 0,
        workspace: "/other",
      },
    });
    expect(created.state.ui.openFiles).toEqual([]);
    expect(created.state.ui.filePaneOpen).toBe(false);
    const left = reduce(created.state, { type: "intent/leaveSession" });
    expect(left.state.active).toBeNull();
    expect(left.state.ui.openFiles).toEqual([]);
    expect(left.state.ui.filePaneOpen).toBe(false);
  });

  it("swaps the file and context sidebars and persists the order", () => {
    const result = reduce(state(), { type: "ui/swapRightPanes" });
    expect(result.state.ui.filePanePosition).toBe("inner");
    expect(result.effects).toEqual([
      {
        type: "persist",
        key: STORAGE_KEYS.filePanePosition,
        value: "inner",
      },
    ]);
  });

  it("drops a stale plugin notice when the next command fails", () => {
    const allowed = {
      ...state(),
      connection: { ...state().connection, features: ["plugins.manage"] },
      plugins: {
        ...state().plugins,
        items: [demoPlugin],
        manage: true,
        lastAction: { id: "demo", enabled: true, changed: true, restartRequired: false },
      },
    };
    const result = reduce(allowed, {
      type: "gateway/commandFailed",
      name: "plugin.disable",
      error: { code: "E_PLUGIN_CONFIG", message: "cannot update allow list" },
    });
    expect(result.state.plugins.lastAction).toBeNull();
  });

  it("drops a stale plugin notice when a new command starts", () => {
    const allowed = {
      ...state(),
      connection: { ...state().connection, features: ["plugins.manage"] },
      plugins: {
        ...state().plugins,
        items: [demoPlugin],
        manage: true,
        lastAction: { id: "demo", enabled: true, changed: true, restartRequired: false },
      },
    };
    const result = reduce(allowed, {
      type: "intent/pluginSetEnabled",
      id: "demo",
      enabled: false,
    });
    expect(result.state.plugins.lastAction).toBeNull();
  });

  it("requests context metadata only so the result fits the frame limit", () => {
    const result = reduce(state(), { type: "intent/refreshContext" });
    expect(result.state.active?.contextLoading).toBe(true);
    expect(result.effects).toContainEqual({
      type: "command",
      name: "session.context",
      args: { include_messages: false },
      sessionId: "sess-1",
    });
  });

  it("loads config before any session exists", () => {
    const { state: next, effects } = reduce(state(), {
      type: "intent/refreshConfig",
      sections: ["agent.llm"],
    });
    expect(next.configuration.loading).toBe(true);
    expect(effects).toEqual([
      {
        type: "command",
        name: "config.get",
        args: {
          sections: ["agent.llm"],
          include_schema: true,
        },
      },
    ]);
  });

  it("allows the first save when the file does not exist", () => {
    const base = state();
    const { effects } = reduce(
      {
        ...base,
        configuration: {
          ...base.configuration,
          writable: true,
          exists: false,
          revision: "",
        },
      },
      {
        type: "intent/saveConfig",
        changes: { "agent.llm.model": "x" },
        expectedRevision: "",
      },
    );
    expect(effects[0]).toMatchObject({ type: "command", name: "config.set" });
  });

  it("blocks saves when the gateway is read-only", () => {
    const base = state();
    const { effects } = reduce(
      {
        ...base,
        configuration: { ...base.configuration, writable: false },
      },
      {
        type: "intent/saveConfig",
        changes: { "agent.llm.model": "x" },
        expectedRevision: "rev",
      },
    );
    expect(effects).toEqual([]);
  });

  it("records a config conflict from the command error", () => {
    const base = state();
    const { state: next } = reduce(
      {
        ...base,
        configuration: { ...base.configuration, saving: true },
      },
      {
        type: "gateway/commandFailed",
        name: "config.set",
        error: {
          code: "E_CONFIG_CONFLICT",
          message: "conflict",
          context: { current_revision: "rev-2" },
        },
      },
    );
    expect(next.configuration.saving).toBe(false);
    expect(next.configuration.conflict?.currentRevision).toBe("rev-2");
  });

  it("records profile command errors for the confirmation flow", () => {
    const base = state();
    const { state: next } = reduce(
      {
        ...base,
        configuration: { ...base.configuration, saving: true },
      },
      {
        type: "gateway/commandFailed",
        name: "config.profile.delete",
        error: {
          code: "E_CONFIG_PROFILE_IN_USE",
          message: "profile is still referenced by historical sessions",
          context: { referenced_by: "sessions", count: 2 },
        },
      },
    );
    expect(next.configuration.saving).toBe(false);
    expect(next.configuration.error?.code).toBe("E_CONFIG_PROFILE_IN_USE");
    expect(next.configuration.error?.context?.count).toBe(2);
  });

  it("disables config editing when a refresh fails", () => {
    const base = state();
    const { state: next } = reduce(
      {
        ...base,
        configuration: { ...base.configuration, writable: true },
      },
      {
        type: "gateway/commandFailed",
        name: "config.get",
        error: {
          code: "E_CONFIG_PARSE",
          message: "bad yaml",
          context: { path: "/tmp/config.yaml" },
        },
      },
    );
    expect(next.configuration.writable).toBe(false);
    expect(next.configuration.loading).toBe(false);
    expect(next.configuration.error?.code).toBe("E_CONFIG_PARSE");
  });

  it("refreshes config after a successful save", () => {
    const base = state();
    const { effects } = reduce(
      {
        ...base,
        configuration: { ...base.configuration, saving: true },
      },
      {
        type: "configuration/saved",
        result: {
          revision: "rev-2",
          changed: true,
          changes: [],
          apply: {
            declared: { live: [], new_session: [], restart: [] },
            runtime: {},
          },
          warnings: [],
        },
      },
    );
    expect(effects).toContainEqual({
      type: "command",
      name: "config.get",
      args: {
        sections: ["agent.llm", "agent.profiles"],
        include_schema: false,
      },
    });
  });

  it("adopts the save revision and clears the saved section draft", () => {
    const base = state();
    const { state: next } = reduce(
      {
        ...base,
        configuration: {
          ...base.configuration,
          saving: true,
          revision: "rev-1",
          draftSections: { "agent.llm": true },
          dirty: true,
        },
      },
      {
        type: "configuration/saved",
        result: {
          revision: "rev-2",
          changed: true,
          changes: [],
          apply: {
            declared: { live: [], new_session: [], restart: [] },
            runtime: {},
          },
          warnings: [],
        },
        sections: ["agent.llm"],
      },
    );
    expect(next.configuration.revision).toBe("rev-2");
    expect(next.configuration.loading).toBe(true);
    expect(next.configuration.draftSections["agent.llm"]).toBe(false);
    expect(next.configuration.dirty).toBe(false);
  });

  it("merges a section load without dropping other loaded sections", () => {
    const base = state();
    const { state: next } = reduce(
      {
        ...base,
        configuration: {
          ...base.configuration,
          fields: {
            "agent.llm.model": {
              value: "gpt-4o-mini",
              source: "file",
              secret: false,
              set: true,
              masked: null,
            },
          },
        },
      },
      {
        type: "configuration/loaded",
        result: {
          revision: "rev-1",
          path: "/tmp/config.yaml",
          exists: true,
          manage: true,
          writable: true,
          schema: null,
          fields: {
            "agent.context.policy": {
              value: "compacting",
              source: "file",
              secret: false,
              set: true,
              masked: null,
            },
          },
          runtimeScopes: { "agent.context.policy": "new_session" },
          issues: [],
        },
        sections: ["agent.context"],
        fieldsIncluded: true,
      },
    );
    expect(next.configuration.fields["agent.llm.model"]?.value).toBe(
      "gpt-4o-mini",
    );
    expect(next.configuration.fields["agent.context.policy"]?.value).toBe(
      "compacting",
    );
    expect(next.configuration.loadedSections["agent.context"]?.loading).toBe(
      false,
    );
  });

  it("keeps fields when a schema-only bootstrap loads", () => {
    const base = state();
    const { state: next } = reduce(
      {
        ...base,
        configuration: {
          ...base.configuration,
          fields: {
            "agent.llm.model": {
              value: "gpt-4o-mini",
              source: "file",
              secret: false,
              set: true,
              masked: null,
            },
          },
        },
      },
      {
        type: "configuration/loaded",
        result: {
          revision: "rev-1",
          path: "/tmp/config.yaml",
          exists: true,
          manage: true,
          writable: true,
          schema: { version: 1, sections: [] },
          fields: {},
          runtimeScopes: {},
          issues: [],
        },
        fieldsIncluded: false,
      },
    );
    expect(next.configuration.fields["agent.llm.model"]?.value).toBe(
      "gpt-4o-mini",
    );
    expect(next.configuration.schema).toEqual({ version: 1, sections: [] });
  });

  it("loads one section and tracks drafts", () => {
    const base = state();
    const loading = reduce(base, {
      type: "intent/loadSection",
      section: "agent.context",
    });
    expect(loading.effects).toEqual([
      {
        type: "command",
        name: "config.get",
        args: {
          sections: ["agent.context"],
          include_schema: false,
        },
      },
    ]);
    expect(loading.state.configuration.loadedSections["agent.context"]?.loading)
      .toBe(true);
    const dirty = reduce(loading.state, {
      type: "intent/configDraft",
      section: "agent.context",
      dirty: true,
    });
    expect(dirty.state.configuration.dirty).toBe(true);
  });

  it("previews with persist=false and records a revision mismatch", () => {
    const base = state();
    const previewing = reduce(
      {
        ...base,
        configuration: {
          ...base.configuration,
          writable: true,
          revision: "rev-1",
        },
      },
      {
        type: "intent/previewConfig",
        section: "agent.context",
        changes: { "agent.context.max_tokens": 64000 },
      },
    );
    expect(previewing.effects[0]).toMatchObject({
      type: "command",
      name: "config.set",
      args: {
        changes: { "agent.context.max_tokens": 64000 },
        expected_revision: "rev-1",
        persist: false,
      },
    });
    expect(previewing.state.configuration.previewing).toBe(true);
    const previewed = reduce(previewing.state, {
      type: "configuration/previewed",
      result: {
        revision: "rev-2",
        changed: true,
        changes: [],
        apply: {
          declared: { live: [], new_session: [], restart: [] },
          runtime: {},
        },
        warnings: [],
      },
      revisionMismatch: true,
    });
    expect(previewed.state.configuration.preview?.revision).toBe("rev-2");
    expect(previewed.state.configuration.previewRevisionMismatch).toBe(true);
    expect(previewed.state.configuration.previewing).toBe(false);
  });

  it("saves a profile with the current revision", () => {
    const base = state();
    const { effects } = reduce({
      ...base,
      configuration: { ...base.configuration, writable: true, revision: "rev-1", exists: true },
    }, {
      type: "intent/saveProfile",
      name: "fast",
      values: { model: "deepseek-chat" },
    });
    expect(effects[0]).toMatchObject({
      type: "command", name: "config.profile.set",
      args: {
        name: "fast", values: { model: "deepseek-chat" }, expected_revision: "rev-1",
      },
    });
  });

  it("passes confirm_in_use when deleting a session-bound profile", () => {
    const base = state();
    const { effects } = reduce({
      ...base,
      configuration: { ...base.configuration, writable: true, revision: "rev-1", exists: true },
    }, {
      type: "intent/deleteProfile", name: "fast", confirmInUse: true,
    });
    expect(effects[0]).toMatchObject({
      type: "command", name: "config.profile.delete",
      args: { name: "fast", expected_revision: "rev-1", confirm_in_use: true },
    });
  });

  it("moves a fallback with its indices", () => {
    const base = state();
    const { effects } = reduce({
      ...base,
      configuration: { ...base.configuration, writable: true, revision: "rev-1", exists: true },
    }, { type: "intent/moveFallback", from: 1, to: 0 });
    expect(effects[0]).toMatchObject({
      type: "command", name: "config.fallback.move",
      args: { from: 1, to: 0, expected_revision: "rev-1" },
    });
  });
});

describe("phase 3 recovery and schema completeness", () => {
  it("marks the schema complete only after an unfiltered response", () => {
    const base = state();
    const scoped = reduce(base, {
      type: "configuration/loaded",
      result: {
        revision: "rev-1",
        path: "/tmp/config.yaml",
        exists: true,
        manage: true,
        writable: true,
        schema: { version: 1, sections: [] },
        fields: {},
        runtimeScopes: {},
        issues: [],
      },
      sections: ["agent.llm"],
      fieldsIncluded: true,
    });
    expect(scoped.state.configuration.schemaComplete).toBe(false);
    const full = reduce(scoped.state, {
      type: "configuration/loaded",
      result: {
        revision: "rev-1",
        path: "/tmp/config.yaml",
        exists: true,
        manage: true,
        writable: true,
        schema: { version: 1, sections: [] },
        fields: {},
        runtimeScopes: {},
        issues: [],
      },
      sections: null,
      fieldsIncluded: false,
    });
    expect(full.state.configuration.schemaComplete).toBe(true);
  });

  it("clears previewing when config.set fails", () => {
    const base = state();
    const { state: next } = reduce(
      {
        ...base,
        configuration: {
          ...base.configuration,
          previewing: true,
          preview: {
            revision: "rev-2", changed: true, changes: [],
            apply: { declared: { live: [], new_session: [], restart: [] }, runtime: {} },
            warnings: [],
          },
        },
      },
      {
        type: "gateway/commandFailed",
        name: "config.set",
        error: { code: "E_CONFIG_ENUM", message: "bad" },
      },
    );
    expect(next.configuration.previewing).toBe(false);
    expect(next.configuration.preview).toBeNull();
  });

  it("records a section load error and lets a later load recover", () => {
    const base = state();
    const failed = reduce(
      { ...base, configuration: { ...base.configuration, writable: true } },
      {
        type: "configuration/sectionError",
        section: "agent.context",
        error: { code: "E_CLIENT", message: "offline" },
      },
    );
    expect(
      failed.state.configuration.loadedSections["agent.context"]?.error?.code,
    ).toBe("E_CLIENT");
    expect(
      failed.state.configuration.loadedSections["agent.context"]?.loading,
    ).toBe(false);
    const recovered = reduce(failed.state, {
      type: "configuration/sectionLoaded",
      section: "agent.context",
      result: {
        revision: "rev-2",
        path: "/tmp/config.yaml",
        exists: true,
        manage: true,
        writable: true,
        schema: null,
        fields: {
          "agent.context.max_tokens": {
            value: 32000, source: "file", secret: false, set: true, masked: null,
          },
        },
        runtimeScopes: { "agent.context.max_tokens": "new_session" },
        issues: [],
      },
    });
    expect(
      recovered.state.configuration.loadedSections["agent.context"]?.error,
    ).toBeNull();
    expect(recovered.state.configuration.writable).toBe(true);
  });
});

describe("phase 3 post-save refresh token", () => {
  it("does not change loadedAtRevision until the section refreshes", () => {
    const base = state();
    const seeded = {
      ...base,
      configuration: {
        ...base.configuration,
        loadedSections: {
          "agent.context": {
            loading: false,
            error: null,
            loadedAtRevision: "rev-1",
          },
        },
      },
    };
    const saved = reduce(seeded, {
      type: "configuration/saved",
      result: {
        revision: "rev-2", changed: true, changes: [],
        apply: { declared: { live: [], new_session: [], restart: [] }, runtime: {} },
        warnings: [],
      },
      sections: ["agent.context"],
    });
    expect(
      saved.state.configuration.loadedSections["agent.context"]?.loadedAtRevision,
    ).toBe("rev-1");
    const refreshed = reduce(saved.state, {
      type: "configuration/loaded",
      result: {
        revision: "rev-2",
        path: "/tmp/config.yaml",
        exists: true,
        manage: true,
        writable: true,
        schema: null,
        fields: {
          "agent.context.max_tokens": {
            value: 64000, source: "file", secret: false, set: true, masked: null,
          },
        },
        runtimeScopes: {},
        issues: [],
      },
      sections: ["agent.context"],
      fieldsIncluded: true,
    });
    expect(
      refreshed.state.configuration.loadedSections["agent.context"]?.loadedAtRevision,
    ).toBe("rev-2");
  });
});

describe("config push (phase 4)", () => {
  function configState(
    overrides: Partial<AppState["configuration"]> = {},
  ): AppState {
    const base = state();
    return {
      ...base,
      connection: { ...base.connection, features: ["config", "config.push"] },
      configuration: {
        ...INITIAL_CONFIGURATION,
        revision: "rev-1",
        loadedSections: {
          "agent.llm": { loading: false, error: null, loadedAtRevision: "rev-1" },
          logging: { loading: false, error: null, loadedAtRevision: "rev-1" },
        },
        ...overrides,
      },
    };
  }

  const loadedResult = {
    revision: "rev-2",
    path: "/tmp/config.yaml",
    exists: true,
    manage: true,
    writable: true,
    schema: null,
    fields: {},
    runtimeScopes: {},
    issues: [],
  };

  it("refreshes loaded sections when idle", () => {
    const result = reduce(configState(), {
      type: "configuration/changed",
      revision: "rev-2",
      keys: [{ key: "logging.level", scope: "live" }],
    });
    expect(result.state.configuration.remoteRevision).toBe("rev-2");
    expect(result.state.configuration.remoteEpoch).toBe(1);
    expect(result.state.configuration.revision).toBe("rev-1");
    expect(result.effects).toHaveLength(1);
    const effect = result.effects[0];
    expect(effect.type).toBe("command");
    if (effect.type === "command") {
      expect(effect.name).toBe("config.get");
      expect(effect.args.sections).toEqual(["agent.llm", "logging"]);
      expect(effect.args.include_schema).toBe(false);
    }
  });

  it("keeps a draft and does not advance the revision when dirty", () => {
    const result = reduce(configState({ dirty: true }), {
      type: "configuration/changed",
      revision: "rev-2",
      keys: [{ key: "agent.llm.model", scope: "new_session" }],
    });
    expect(result.state.configuration.revision).toBe("rev-1");
    expect(result.state.configuration.remoteRevision).toBe("rev-2");
    expect(result.state.configuration.remoteChangedKeys).toEqual([
      { key: "agent.llm.model", scope: "new_session" },
    ]);
    expect(result.effects).toEqual([]);
  });

  it("ignores its own push for the same revision", () => {
    const result = reduce(configState({ remoteEpoch: 3 }), {
      type: "configuration/changed",
      revision: "rev-1",
      keys: [{ key: "agent.llm.model", scope: "new_session" }],
    });
    expect(result.state.configuration.remoteEpoch).toBe(3);
    expect(result.effects).toEqual([]);
  });

  it("records but does not refresh while saving", () => {
    const result = reduce(configState({ saving: true }), {
      type: "configuration/changed",
      revision: "rev-2",
      keys: [{ key: "logging.level", scope: "live" }],
    });
    expect(result.state.configuration.remoteRevision).toBe("rev-2");
    expect(result.state.configuration.remoteEpoch).toBe(1);
    expect(result.effects).toEqual([]);
  });

  it("records but does not refresh while a read is in flight", () => {
    const result = reduce(configState({ loading: true }), {
      type: "configuration/changed",
      revision: "rev-2",
      keys: [{ key: "logging.level", scope: "live" }],
    });
    expect(result.state.configuration.remoteRevision).toBe("rev-2");
    expect(result.state.configuration.remoteEpoch).toBe(1);
    expect(result.effects).toEqual([]);
  });

  it("clears remote state when the read covered every push", () => {
    const result = reduce(
      configState({
        remoteRevision: "rev-2",
        remoteChangedKeys: [{ key: "logging.level", scope: "live" }],
        remoteEpoch: 1,
      }),
      {
        type: "configuration/loaded",
        result: loadedResult,
        sections: ["agent.llm", "logging"],
        fieldsIncluded: true,
        remoteEpochAtRequest: 1,
      },
    );
    expect(result.state.configuration.remoteRevision).toBeNull();
    expect(result.state.configuration.remoteChangedKeys).toEqual([]);
    expect(result.state.configuration.revision).toBe("rev-2");
    expect(result.effects).toEqual([]);
  });

  it("marks every schema section loaded after a full schema+fields refresh", () => {
    const result = reduce(configState({ loadedSections: {} }), {
      type: "configuration/loaded",
      result: {
        ...loadedResult,
        schema: {
          version: 1,
          sections: [
            {
              key: "agent.context",
              title: "Context",
              title_i18n: {},
              fields: [],
            },
            {
              key: "logging",
              title: "Logging",
              title_i18n: {},
              fields: [],
            },
          ],
        },
        fields: {
          "agent.context.max_tokens": {
            value: 32000, source: "file", secret: false, set: true, masked: null,
          },
        },
      },
      sections: null,
      fieldsIncluded: true,
      remoteEpochAtRequest: 0,
    });

    expect(Object.keys(result.state.configuration.loadedSections).sort()).toEqual([
      "agent.context", "logging",
    ]);
    expect(
      result.state.configuration.loadedSections["agent.context"]?.loadedAtRevision,
    ).toBe("rev-2");
  });

  it("auto-refreshes every section after a full bootstrap, then a push", () => {
    const boot = reduce(configState({ loadedSections: {} }), {
      type: "configuration/loaded",
      result: {
        ...loadedResult,
        schema: {
          version: 1,
          sections: [
            { key: "agent.llm", title: "Model", title_i18n: {}, fields: [] },
            { key: "logging", title: "Logging", title_i18n: {}, fields: [] },
          ],
        },
      },
      sections: null,
      fieldsIncluded: true,
      remoteEpochAtRequest: 0,
    });
    expect(Object.keys(boot.state.configuration.loadedSections).sort()).toEqual([
      "agent.llm", "logging",
    ]);

    const pushed = reduce(boot.state, {
      type: "configuration/changed",
      revision: "rev-3",
      keys: [{ key: "logging.level", scope: "live" }],
    });
    expect(pushed.state.configuration.remoteRevision).toBe("rev-3");
    expect(pushed.effects).toHaveLength(1);
    const effect = pushed.effects[0];
    expect(effect.type).toBe("command");
    if (effect.type === "command") {
      expect(effect.name).toBe("config.get");
      expect([...(effect.args.sections as string[])].sort()).toEqual([
        "agent.llm", "logging",
      ]);
    }
  });

  it("still blocks auto-refresh when a draft exists after the bootstrap", () => {
    const boot = reduce(configState({ loadedSections: {} }), {
      type: "configuration/loaded",
      result: {
        ...loadedResult,
        schema: {
          version: 1,
          sections: [
            { key: "agent.context", title: "Context", title_i18n: {}, fields: [] },
          ],
        },
      },
      sections: null,
      fieldsIncluded: true,
      remoteEpochAtRequest: 0,
    });
    const drafted = reduce(boot.state, {
      type: "configuration/draft",
      section: "agent.context",
      dirty: true,
    });

    const pushed = reduce(drafted.state, {
      type: "configuration/changed",
      revision: "rev-3",
      keys: [{ key: "agent.context.max_tokens", scope: "new_session" }],
    });
    expect(pushed.state.configuration.remoteRevision).toBe("rev-3");
    expect(pushed.state.configuration.revision).toBe("rev-2");
    expect(pushed.effects).toEqual([]);
  });

  it("keeps remote state when a scoped read missed a loaded section", () => {
    const result = reduce(
      configState({
        remoteRevision: "rev-2",
        remoteChangedKeys: [{ key: "logging.level", scope: "live" }],
        remoteEpoch: 1,
      }),
      {
        type: "configuration/loaded",
        result: loadedResult,
        sections: ["agent.llm"],
        fieldsIncluded: true,
        remoteEpochAtRequest: 1,
      },
    );
    expect(result.state.configuration.remoteRevision).toBe("rev-2");
    expect(result.state.configuration.remoteChangedKeys).toEqual([
      { key: "logging.level", scope: "live" },
    ]);
    expect(result.effects).toEqual([]);
  });

  it("refetches once when a push arrived during the read", () => {
    const result = reduce(
      configState({
        remoteRevision: "rev-2",
        remoteChangedKeys: [{ key: "logging.level", scope: "live" }],
        remoteEpoch: 1,
      }),
      {
        type: "configuration/loaded",
        result: loadedResult,
        sections: ["agent.llm", "logging"],
        fieldsIncluded: true,
        remoteEpochAtRequest: 0,
      },
    );
    expect(result.state.configuration.remoteRevision).toBe("rev-2");
    expect(result.state.configuration.remoteEpoch).toBe(1);
    expect(result.effects).toHaveLength(1);
    const effect = result.effects[0];
    expect(effect.type).toBe("command");
    if (effect.type === "command") {
      expect(effect.name).toBe("config.get");
    }
  });

  it("stays stale when a push arrived during the read and a draft exists", () => {
    const result = reduce(
      configState({
        dirty: true,
        remoteRevision: "rev-2",
        remoteChangedKeys: [{ key: "logging.level", scope: "live" }],
        remoteEpoch: 1,
      }),
      {
        type: "configuration/loaded",
        result: loadedResult,
        sections: ["agent.llm", "logging"],
        fieldsIncluded: true,
        remoteEpochAtRequest: 0,
      },
    );
    expect(result.state.configuration.remoteRevision).toBe("rev-2");
    expect(result.effects).toEqual([]);
  });
});
