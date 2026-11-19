/**
 * Plugin enablement vs. runtime state.
 *
 * `plugin.list` exposes two independent facts:
 * - `enabled`: whether the plugin is in the file-level `agent.plugins.allow`;
 * - `state`: what the running host currently has loaded.
 *
 * Enabling writes the allow list only; the plugin is loaded on the next
 * gateway start. Until then the pair is inconsistent (`enabled: true,
 * state: "disabled"`), which the UI must render as "restart pending" rather
 * than literally showing "disabled" next to "enabled".
 */

import type { PluginSummary } from "../protocol/commands";

export type PluginRuntimeBadge =
  | "restart"
  | "active"
  | "loading"
  | "quiescing"
  | "failed"
  | "disabled"
  | "discovered"
  | "unloaded"
  | "invalid"
  | "unknown";

type PluginStateFields = Pick<PluginSummary, "enabled" | "state">;

const LOADED_STATES = new Set(["active", "loading", "quiescing"]);
const INACTIVE_STATES = new Set(["disabled", "discovered", "unloaded"]);

const KNOWN_STATES: ReadonlySet<string> = new Set([
  ...LOADED_STATES,
  ...INACTIVE_STATES,
  "failed",
  "invalid",
]);

/**
 * Mirror the gateway's `plugin.enable` / `plugin.disable` edit on the parsed
 * file-level allow list: appends new ids and removes disabled ones in order.
 * Returns `null` when the gateway did not expose the list.
 */
export function withAllowed(
  allow: readonly string[] | null,
  id: string,
  enabled: boolean,
): string[] | null {
  if (allow === null) return null;
  if (enabled) return allow.includes(id) ? [...allow] : [...allow, id];
  return allow.filter((existing) => existing !== id);
}

/** True when the saved enablement and the running state disagree. */
export function pluginNeedsRestart(plugin: PluginStateFields): boolean {
  if (plugin.enabled === undefined) return false;
  const state = plugin.state ?? "";
  return plugin.enabled
    ? INACTIVE_STATES.has(state)
    : LOADED_STATES.has(state);
}

/** A stable key the UI can map to localized copy. */
export function pluginRuntimeBadge(plugin: PluginStateFields): PluginRuntimeBadge {
  if (pluginNeedsRestart(plugin)) return "restart";
  const state = plugin.state ?? "";
  return KNOWN_STATES.has(state) ? (state as PluginRuntimeBadge) : "unknown";
}
