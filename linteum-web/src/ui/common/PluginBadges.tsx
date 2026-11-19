import { useStrings } from "../../hooks/useStrings";
import type { PluginSummary } from "../../protocol/commands";
import { pluginRuntimeBadge, type PluginRuntimeBadge } from "../../state/plugins";
import type { Strings } from "../../strings";

function stateLabel(strings: Strings, badge: PluginRuntimeBadge): string {
  const detail = strings.pluginDetail;
  switch (badge) {
    case "active":
      return detail.stateActive;
    case "loading":
      return detail.stateLoading;
    case "quiescing":
      return detail.stateQuiescing;
    case "failed":
      return detail.stateFailed;
    case "disabled":
      return detail.stateDisabled;
    case "discovered":
      return detail.stateDiscovered;
    case "unloaded":
      return detail.stateUnloaded;
    case "invalid":
      return detail.stateInvalid;
    default:
      return detail.none;
  }
}

/**
 * File-level enablement plus the runtime state when it is meaningful.
 *
 * A saved enable/disable that the running host has not picked up yet renders
 * as "restart required" — never as a bare `disabled` next to `enabled`.
 */
export function PluginStateBadges({
  plugin,
  showState = false,
}: {
  plugin: PluginSummary;
  showState?: boolean;
}) {
  const strings = useStrings();
  const badge = pluginRuntimeBadge(plugin);

  // Older gateways do not report file-level enablement; show only the
  // runtime state instead of guessing "disabled".
  if (plugin.enabled === undefined) {
    if (!plugin.state) return null;
    return (
      <span className="badge">
        {badge === "unknown" ? plugin.state : stateLabel(strings, badge)}
      </span>
    );
  }

  return (
    <>
      <span className={`badge${plugin.enabled ? " badge-success" : ""}`}>
        {plugin.enabled
          ? strings.settings.pluginEnabled
          : strings.settings.pluginDisabled}
      </span>
      {badge === "restart" ? (
        <span className="badge badge-warning" title={strings.pluginDetail.restartHint}>
          {strings.pluginDetail.restartPending}
        </span>
      ) : showState && plugin.state ? (
        <span className="badge">
          {badge === "unknown" ? plugin.state : stateLabel(strings, badge)}
        </span>
      ) : null}
    </>
  );
}
