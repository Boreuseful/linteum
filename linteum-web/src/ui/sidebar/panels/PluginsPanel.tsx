import { Puzzle, RefreshCw } from "lucide-react";
import { useEffect } from "react";
import { useStrings } from "../../../hooks/useStrings";
import { store, useAppState } from "../../../hooks/useStore";
import { controller } from "../../../transport/controller";
import { PanelEmpty, PanelLoading } from "../../common/EmptyState";
import { PluginStateBadges } from "../../common/PluginBadges";
import type { PanelProps } from "../types";

export function PluginsPanel(_props: PanelProps) {
  const strings = useStrings();
  const state = useAppState();
  const plugins = state.plugins;
  const open = state.ui.openPanels.plugins === true;

  useEffect(() => {
    if (open) controller.refreshPlugins();
  }, [open]);

  const openPlugin = (pluginId: string) =>
    store.dispatch({
      type: "ui/openOverlay",
      overlay: { kind: "plugin-detail", pluginId },
    });

  return (
    <>
      <div className="panel-toolbar">
        <button
          className="btn btn-sm"
          onClick={() => controller.refreshPlugins()}
          disabled={plugins.loading}
        >
          <RefreshCw size={13} /> {strings.sidebar.refresh}
        </button>
      </div>

      {plugins.loading ? <PanelLoading label={strings.sidebar.loading} /> : null}

      {!plugins.loading && plugins.error ? (
        <PanelEmpty>
          {plugins.error.code}: {plugins.error.message}
        </PanelEmpty>
      ) : null}

      {!plugins.loading && !plugins.error && plugins.items.length === 0 ? (
        <PanelEmpty>{strings.sidebar.noPlugins}</PanelEmpty>
      ) : null}

      {plugins.items.map((plugin) => (
        <div
          className="task-row plugin-row"
          key={plugin.id}
          role="button"
          tabIndex={0}
          aria-label={`${strings.pluginDetail.open}: ${plugin.name ?? plugin.id}`}
          onClick={() => openPlugin(plugin.id)}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              openPlugin(plugin.id);
            }
          }}
        >
          <div className="task-row-head">
            <Puzzle size={14} />
            <span className="task-name">{plugin.name ?? plugin.id}</span>
            <PluginStateBadges plugin={plugin} />
          </div>
          <div className="task-meta">
            <span className="mono">{plugin.id}</span>
            {plugin.version ? <span>v{plugin.version}</span> : null}
          </div>
          {plugin.description ? (
            <div className="small muted">{plugin.description}</div>
          ) : null}
        </div>
      ))}

      <div className="small muted">{strings.sidebar.pluginReadOnly}</div>
    </>
  );
}
