/** Runtime configuration for the browser client. */

export const CLIENT_NAME = "linteum-web";
export const CLIENT_VERSION = "0.1.0";
export const PROTOCOL_VERSION = 1;

export const DEFAULT_GATEWAY_URL =
  (import.meta.env.VITE_GATEWAY_URL as string | undefined) ?? "ws://127.0.0.1:8765/";

export const COMMAND_TIMEOUT_MS = 30_000;
export const HELLO_TIMEOUT_MS = 10_000;
/** Replay of a long WAL can legitimately outlive the normal command timeout. */
export const SUBSCRIBE_TIMEOUT_MS = 120_000;
export const RECONNECT_BASE_MS = 500;
export const RECONNECT_MAX_MS = 8_000;
export const PING_INTERVAL_MS = 25_000;

export const ARTIFACT_CHUNK_BYTES = 65_536;
export const MAX_ARTIFACT_BYTES = 2 * 1024 * 1024;
export const MAX_INLINE_OUTPUT_CHARS = 20_000;
export const MAX_TRANSCRIPT_ITEMS = 2_000;
export const MAX_ACTIVITY_ENTRIES = 500;
export const MAX_SEEN_FRAMES = 5_000;

/** Sidebar geometry. Must match `--rail-left-width` / `--rail-right-width`. */
export const DEFAULT_LEFT_RAIL_WIDTH = 264;
export const DEFAULT_RIGHT_RAIL_WIDTH = 336;
export const MIN_LEFT_RAIL_WIDTH = 200;
export const MAX_LEFT_RAIL_WIDTH = 480;
export const MIN_RIGHT_RAIL_WIDTH = 240;
export const MAX_RIGHT_RAIL_WIDTH = 560;

/** File sidebar geometry (the pane that opens beside the context rail). */
export const DEFAULT_FILE_PANE_WIDTH = 460;
export const MIN_FILE_PANE_WIDTH = 280;
export const MAX_FILE_PANE_WIDTH = 760;

export const STORAGE_KEYS = {
  theme: "linteum.theme",
  locale: "linteum.locale",
  gatewayUrl: "linteum.gatewayUrl",
  lastSession: "linteum.lastSession",
  lastWorkspace: "linteum.lastWorkspace",
  panelMode: "linteum.panelMode",
  openPanels: "linteum.openPanels",
  rightRail: "linteum.rightRailOpen",
  leftRailWidth: "linteum.railLeftWidth",
  rightRailWidth: "linteum.railRightWidth",
  filePaneWidth: "linteum.filePaneWidth",
  filePanePosition: "linteum.filePanePosition",
  draft: (sessionId: string) => `linteum.draft.${sessionId}`,
} as const;

export const DEFAULT_WORKSPACE = "/workspace";
