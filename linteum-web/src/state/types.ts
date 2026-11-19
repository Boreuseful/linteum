import type { ErrorInfo } from "../protocol/envelope";
import type { WorkspaceFileResult } from "../workspace";
import type {
  ConfigGetResult,
  ConfigSetResult,
  ContextInfo,
  ModeInfo,
  PluginChange,
  PluginSummary,
  SessionInfo,
  Usage,
} from "../protocol/commands";
import type { ConfigurationState } from "./configuration";
import type { Locale } from "../strings";
import type {
  ApprovalInfo,
  BackgroundTaskInfo,
  ConfigChangedKey,
  SubagentInfo,
  ToolCallInfo,
  ViewFrame,
} from "../protocol/frames";

export type ThemeChoice = "dark" | "light" | "mono" | "system";

export type ConnectionStatus =
  | "idle"
  | "connecting"
  | "handshaking"
  | "ready"
  | "reconnecting"
  | "closed"
  | "error";

export interface ConnectionState {
  status: ConnectionStatus;
  url: string;
  instanceId: string | null;
  protocol: number | null;
  features: string[];
  limits: Record<string, number>;
  error: ErrorInfo | null;
  retryAt: number | null;
}

export interface SessionCatalogState {
  items: SessionInfo[];
  loading: boolean;
  error: ErrorInfo | null;
}

export interface ActivityEntry {
  id: string;
  ts: number;
  kind: "connection" | "notice" | "error" | "flow" | "gap" | "debug";
  message: string;
}

export interface ArtifactState {
  ref: string;
  chunks: string[];
  size: number;
  eof: boolean;
  loading: boolean;
  error: ErrorInfo | null;
}

export type OverlayState =
  | { kind: "new-session" }
  | { kind: "session-picker" }
  | { kind: "command-palette" }
  | { kind: "help" }
  | { kind: "mode-picker" }
  | { kind: "plugin-detail"; pluginId: string }
  | { kind: "child-context"; childId: string; title?: string }
  | { kind: "artifact"; ref: string; title?: string };

export type UiPage = "chat" | "settings";

/** Where the file sidebar sits relative to the context sidebar. */
export type FilePanePosition = "outer" | "inner";

/** One open editor tab in the file sidebar. Content is cached after the read. */
export interface OpenFileTab {
  path: string;
  /** Workspace root the path was opened from; kept for retries. */
  root: string;
  name: string;
  loading: boolean;
  text?: string;
  binary?: boolean;
  size?: number;
  truncated?: boolean;
  error?: string;
}

export interface UiState {
  page: UiPage;
  theme: ThemeChoice;
  locale: Locale;
  leftRailOpen: boolean;
  rightRailOpen: boolean;
  leftRailWidth: number;
  rightRailWidth: number;
  /** Independent file sidebar, shown beside the context rail. */
  filePaneOpen: boolean;
  filePaneWidth: number;
  filePanePosition: FilePanePosition;
  openFiles: OpenFileTab[];
  activeFilePath: string | null;
  panelMode: "stack" | "tabs";
  openPanels: Record<string, boolean>;
  activePanelId: string | null;
  overlays: OverlayState[];
  composer: string;
  activityOpen: boolean;
  activity: ActivityEntry[];
  artifacts: Record<string, ArtifactState>;
}

export interface ItemBase {
  id: string;
  seq: number;
  ordinal: number;
  createdAt: number;
}

export interface UserItem extends ItemBase {
  kind: "user";
  text: string;
  clientMessageId?: string;
  eventId?: string;
  traceId?: string;
}

export interface AssistantItem extends ItemBase {
  kind: "assistant";
  text: string;
  reasoning: string;
  streaming: boolean;
  interrupted: boolean;
  provider?: string;
  model?: string;
  usage: Usage;
  cost: Usage;
  finish?: string;
  toolCalls: ToolCallInfo[];
}

export type ToolStatus =
  | "requested"
  | "running"
  | "ok"
  | "failed"
  | "backgrounded"
  | "queued";

export interface ToolItem extends ItemBase {
  kind: "tool";
  callId: string;
  name: string;
  args?: unknown;
  status: ToolStatus;
  output?: unknown;
  outputRef?: string | null;
  code?: string | null;
  error?: string | null;
  errorInfo?: ErrorInfo | null;
  sideEffect?: string;
  background?: boolean;
  taskId?: string | null;
  startedAt?: number;
  finishedAt?: number;
}

export interface SubagentItem extends ItemBase {
  kind: "subagent";
  subagent: SubagentInfo;
}

export interface CompactionItem extends ItemBase {
  kind: "compaction";
  status: "started" | "applied";
  fromIndex?: number;
  toIndex?: number;
  summary?: string;
  summaryRef?: string;
  tokens?: number;
  fallback?: boolean;
  errorInfo?: ErrorInfo | null;
}

export interface NoticeItem extends ItemBase {
  kind: "notice";
  noticeKind: string;
  payload: Record<string, unknown>;
}

export interface ErrorItem extends ItemBase {
  kind: "error";
  errorInfo: ErrorInfo;
  code?: string | null;
  round?: number;
}

export type TranscriptItem =
  | UserItem
  | AssistantItem
  | ToolItem
  | SubagentItem
  | CompactionItem
  | NoticeItem
  | ErrorItem;

export interface StreamingState {
  requestId?: string;
  kind: "text" | "reasoning";
  text: string;
}

export interface ActiveSessionState {
  id: string;
  info: SessionInfo | null;
  phase: string;
  round: number;
  busy: boolean;
  pending: string[];
  streaming: StreamingState | null;
  usage: Usage;
  boundModel: string | null;
  mode: ModeInfo | null;
  contextDigest: string | null;
  lastError: ErrorInfo | null;
  lastSeq: number;
  /**
   * True while the session is replaying history after `session.subscribe`.
   * The transport buffers frames until the subscribe result arrives, then
   * commits them in one transition so the transcript never grows in chunks.
   */
  hydrating: boolean;
  seen: Set<string>;
  transcript: TranscriptItem[];
  approvals: ApprovalInfo[];
  background: Record<string, BackgroundTaskInfo>;
  subagents: Record<string, SubagentInfo>;
  childSessions: SessionInfo[];
  modes: ModeInfo[];
  modesLoading: boolean;
  context: ContextInfo | null;
  contextLoading: boolean;
  contextError: ErrorInfo | null;
  tasksLoading: boolean;
}

/** Result of the last successful `plugin.enable` / `plugin.disable`. */
export interface PluginManageResult {
  id: string;
  enabled: boolean;
  changed: boolean;
  restartRequired: boolean;
}

/** Result of `plugin.set_allowed` with `mode="apply"`. */
export interface PluginApplyResult {
  restartRequired: boolean;
  changes: PluginChange[];
}

/**
 * Gateway-wide plugin catalogue. `plugin.list` takes no session, so this is
 * global state: the settings page can load it even before a session exists.
 */
export interface PluginsState {
  items: PluginSummary[];
  loading: boolean;
  manage: boolean;
  /** File-level `agent.plugins.allow`; `null` when the gateway omits it. */
  allow: string[] | null;
  /** A runtime `plugin.set_allowed(mode="apply")` is in flight. */
  applying: boolean;
  lastApply: PluginApplyResult | null;
  error: ErrorInfo | null;
  /** Plugin id whose enable/disable command is in flight. */
  pendingId: string | null;
  lastAction: PluginManageResult | null;
}

export interface AppState {
  connection: ConnectionState;
  catalog: SessionCatalogState;
  active: ActiveSessionState | null;
  plugins: PluginsState;
  configuration: ConfigurationState;
  ui: UiState;
}

export interface CommandEffect {
  type: "command";
  name: string;
  args: Record<string, unknown>;
  sessionId?: string;
  timeoutMs?: number;
  restoreDraft?: string;
}

export type Effect =
  | CommandEffect
  | { type: "connect"; url: string }
  | { type: "disconnect" }
  | { type: "workspaceRead"; root: string; path: string }
  | { type: "persist"; key: string; value: string | null };

export type Action =
  | { type: "gateway/status"; status: ConnectionStatus; error?: ErrorInfo | null; retryAt?: number | null }
  | { type: "gateway/url"; url: string }
  | { type: "gateway/hello"; hello: { instance_id: string; protocol: number; features: string[]; limits: Record<string, number> } }
  | { type: "gateway/frame"; frame: ViewFrame }
  | {
      type: "gateway/commandFailed";
      name: string;
      error: ErrorInfo;
      draftText?: string;
      ref?: string;
      sessionId?: string;
    }
  | { type: "gateway/unknownFrame"; name: string }
  | { type: "catalog/loading" }
  | { type: "catalog/loaded"; sessions: SessionInfo[]; limit: number; offset: number; append: boolean }
  | { type: "catalog/error"; error: ErrorInfo }
  | { type: "session/created"; info: SessionInfo }
  | {
      type: "session/subscribed";
      sessionId: string;
      snapshotSeq: number;
      /** Frames buffered while the subscription replay was in flight. */
      frames: ViewFrame[];
    }
  | { type: "session/modesLoaded"; modes: ModeInfo[] }
  | { type: "session/contextLoading" }
  | { type: "session/contextLoaded"; context: ContextInfo }
  | { type: "session/contextError"; error: ErrorInfo }
  | { type: "session/tasksLoading" }
  | { type: "session/tasksLoaded"; sessionId: string; tasks: BackgroundTaskInfo[] }
  | {
      type: "plugins/loaded";
      plugins: PluginSummary[];
      manage: boolean;
      allow: string[] | null;
    }
  | {
      type: "plugins/managed";
      plugin: PluginSummary;
      changed: boolean;
      restartRequired: boolean;
    }
  | {
      type: "plugins/applied";
      allow: string[];
      plugins: PluginSummary[];
      restartRequired: boolean;
      changes: PluginChange[];
    }
  | { type: "configuration/loading" }
  | {
      type: "configuration/loaded";
      result: ConfigGetResult;
      sections?: string[] | null;
      fieldsIncluded?: boolean;
      /** ``remoteEpoch`` when the controller issued this ``config.get``. */
      remoteEpochAtRequest?: number;
    }
  | {
      type: "configuration/changed";
      revision: string;
      keys: ConfigChangedKey[];
    }
  | {
      type: "configuration/saved";
      result: ConfigSetResult;
      sections?: string[];
    }
  | { type: "configuration/sectionLoading"; section: string }
  | {
      type: "configuration/sectionLoaded";
      section: string;
      result: ConfigGetResult;
    }
  | {
      type: "configuration/sectionError";
      section: string;
      error: ErrorInfo;
    }
  | { type: "configuration/draft"; section: string; dirty: boolean }
  | {
      type: "configuration/previewed";
      result: ConfigSetResult;
      revisionMismatch: boolean;
    }
  | { type: "configuration/previewCleared" }
  | { type: "configuration/error"; error: ErrorInfo }
  | { type: "configuration/conflict"; currentRevision: string | null }
  | { type: "configuration/reset" }
  | { type: "session/childrenLoaded"; parentSessionId: string; sessions: SessionInfo[] }
  | { type: "intent/send"; text: string }
  | { type: "intent/interrupt" }
  | { type: "intent/approvalDecide"; callId: string; decision: "grant" | "deny"; reason?: string }
  | { type: "intent/taskStop"; taskId: string }
  | { type: "intent/modeSet"; modeName: string }
  | { type: "file/open"; root: string; path: string; name: string }
  | { type: "file/loaded"; root: string; path: string; result: WorkspaceFileResult }
  | { type: "file/activate"; path: string }
  | { type: "file/close"; path: string }
  | { type: "file/paneToggle" }
  | { type: "ui/filePaneWidth"; width: number; commit?: boolean }
  | { type: "ui/swapRightPanes" }
  | { type: "intent/createSession"; workspace: string; title?: string; profile?: string }
  | { type: "intent/openSession"; sessionId: string; info?: SessionInfo | null }
  | { type: "intent/leaveSession" }
  | { type: "intent/refreshSessions"; workspace?: string }
  | { type: "intent/refreshContext" }
  | { type: "intent/refreshTasks" }
  | { type: "intent/refreshModes" }
  | { type: "intent/refreshPlugins" }
  | { type: "intent/pluginSetEnabled"; id: string; enabled: boolean }
  | { type: "intent/applyPlugins" }
  | {
      type: "intent/refreshConfig";
      sections?: string[];
      includeSchema?: boolean;
      includeFields?: boolean;
    }
  | { type: "intent/loadSection"; section: string }
  | { type: "intent/configDraft"; section: string; dirty: boolean }
  | {
      type: "intent/previewConfig";
      section: string;
      changes: Record<string, unknown>;
    }
  | {
      type: "intent/saveConfig";
      changes: Record<string, unknown>;
      expectedRevision: string;
    }
  | { type: "intent/unsetConfig"; keys: string[]; expectedRevision: string }
  | { type: "intent/saveProfile"; name: string; values: Record<string, unknown> }
  | { type: "intent/deleteProfile"; name: string; confirmInUse?: boolean }
  | { type: "intent/duplicateProfile"; from: string; name: string }
  | { type: "intent/saveFallback"; values: Record<string, unknown>; index?: number }
  | { type: "intent/deleteFallback"; index: number }
  | { type: "intent/moveFallback"; from: number; to: number }
  | { type: "intent/refreshChildren" }
  | { type: "intent/readArtifact"; ref: string; offset?: number; limit?: number }
  | { type: "intent/resume" }
  | { type: "ui/page"; page: UiPage }
  | {
      type: "ui/railWidth";
      side: "left" | "right";
      width: number;
      /** Persist the width; false while a drag is in flight. */
      commit?: boolean;
    }
  | { type: "ui/theme"; theme: ThemeChoice }
  | { type: "ui/locale"; locale: Locale }
  | { type: "ui/toggleLeftRail" }
  | { type: "ui/toggleRightRail" }
  | { type: "ui/togglePanel"; panelId: string }
  | { type: "ui/panelMode"; mode: "stack" | "tabs" }
  | { type: "ui/selectPanel"; panelId: string }
  | { type: "ui/openOverlay"; overlay: OverlayState }
  | { type: "ui/closeOverlay"; kind?: OverlayState["kind"] }
  | { type: "ui/composer"; text: string }
  | { type: "ui/activityToggle" }
  | { type: "ui/activityClear" }
  | { type: "ui/clearError" };
