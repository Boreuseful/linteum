import {
  DEFAULT_FILE_PANE_WIDTH,
  DEFAULT_GATEWAY_URL,
  DEFAULT_LEFT_RAIL_WIDTH,
  DEFAULT_RIGHT_RAIL_WIDTH,
  MAX_FILE_PANE_WIDTH,
  MAX_LEFT_RAIL_WIDTH,
  MAX_RIGHT_RAIL_WIDTH,
  MIN_FILE_PANE_WIDTH,
  MIN_LEFT_RAIL_WIDTH,
  MIN_RIGHT_RAIL_WIDTH,
  STORAGE_KEYS,
} from "../config";
import { resolveLocale } from "../i18n";
import { INITIAL_CONFIGURATION } from "./configuration";
import { reduce } from "./reducer";
import { defaultOpenPanels } from "./sidebar";
import { AppStore } from "./store";
import type { AppState, ThemeChoice } from "./types";

function readTheme(): ThemeChoice {
  try {
    const raw = localStorage.getItem(STORAGE_KEYS.theme);
    if (raw === "dark" || raw === "light" || raw === "mono" || raw === "system") {
      return raw;
    }
  } catch {
    // localStorage can be unavailable in private/embedded contexts.
  }
  return "dark";
}

function readLocale(): ReturnType<typeof resolveLocale> {
  let stored: string | null = null;
  try {
    stored = localStorage.getItem(STORAGE_KEYS.locale);
  } catch {
    // localStorage can be unavailable in private/embedded contexts.
  }
  const language = typeof navigator === "undefined" ? null : navigator.language;
  return resolveLocale(stored, language);
}

function readGatewayUrl(): string {
  try {
    return localStorage.getItem(STORAGE_KEYS.gatewayUrl) || DEFAULT_GATEWAY_URL;
  } catch {
    return DEFAULT_GATEWAY_URL;
  }
}

function readPanelMode(): "stack" | "tabs" {
  try {
    return localStorage.getItem(STORAGE_KEYS.panelMode) === "tabs" ? "tabs" : "stack";
  } catch {
    return "stack";
  }
}

function readOpenPanels(): Record<string, boolean> {
  const defaults = defaultOpenPanels();
  try {
    const raw = localStorage.getItem(STORAGE_KEYS.openPanels);
    if (!raw) return defaults;
    const parsed: unknown = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return { ...defaults, ...(parsed as Record<string, boolean>) };
    }
  } catch {
    // Ignore malformed preferences.
  }
  return defaults;
}

function readRightRail(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEYS.rightRail) !== "false";
  } catch {
    return true;
  }
}

function readFilePanePosition(): "outer" | "inner" {
  try {
    return localStorage.getItem(STORAGE_KEYS.filePanePosition) === "inner"
      ? "inner"
      : "outer";
  } catch {
    return "outer";
  }
}

function readRailWidth(key: string, fallback: number, min: number, max: number): number {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return fallback;
    const value = Number.parseInt(raw, 10);
    if (!Number.isFinite(value)) return fallback;
    return Math.min(max, Math.max(min, value));
  } catch {
    return fallback;
  }
}

export const initialState: AppState = {
  connection: {
    status: "idle",
    url: readGatewayUrl(),
    instanceId: null,
    protocol: null,
    features: [],
    limits: {},
    error: null,
    retryAt: null,
  },
  catalog: {
    items: [],
    loading: false,
    error: null,
  },
  active: null,
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
    theme: readTheme(),
    locale: readLocale(),
    leftRailOpen: true,
    rightRailOpen: readRightRail(),
    leftRailWidth: readRailWidth(
      STORAGE_KEYS.leftRailWidth,
      DEFAULT_LEFT_RAIL_WIDTH,
      MIN_LEFT_RAIL_WIDTH,
      MAX_LEFT_RAIL_WIDTH,
    ),
    rightRailWidth: readRailWidth(
      STORAGE_KEYS.rightRailWidth,
      DEFAULT_RIGHT_RAIL_WIDTH,
      MIN_RIGHT_RAIL_WIDTH,
      MAX_RIGHT_RAIL_WIDTH,
    ),
    filePaneOpen: false,
    filePaneWidth: readRailWidth(
      STORAGE_KEYS.filePaneWidth,
      DEFAULT_FILE_PANE_WIDTH,
      MIN_FILE_PANE_WIDTH,
      MAX_FILE_PANE_WIDTH,
    ),
    filePanePosition: readFilePanePosition(),
    openFiles: [],
    activeFilePath: null,
    panelMode: readPanelMode(),
    openPanels: readOpenPanels(),
    activePanelId: null,
    overlays: [],
    composer: "",
    activityOpen: false,
    activity: [],
    artifacts: {},
  },
};

export const store = new AppStore(reduce, initialState);
