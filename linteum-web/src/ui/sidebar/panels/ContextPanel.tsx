import { RefreshCw } from "lucide-react";
import { useEffect } from "react";
import { useStrings } from "../../../hooks/useStrings";
import { useAppState } from "../../../hooks/useStore";
import { controller } from "../../../transport/controller";
import { PanelEmpty, PanelLoading } from "../../common/EmptyState";
import { formatTokens } from "../../common/format";
import { Stat } from "../../common/Stat";
import type { PanelProps } from "../types";

export function ContextPanel(_props: PanelProps) {
  const strings = useStrings();
  const state = useAppState();
  const active = state.active;
  // `openPanels` only drives the stacked layout; in tabs mode the selected
  // tab is what makes this panel visible (and therefore worth fetching for).
  const open =
    state.ui.panelMode === "tabs"
      ? state.ui.activePanelId === "context"
      : state.ui.openPanels.context === true;

  useEffect(() => {
    if (open && active && !active.context && !active.contextLoading) {
      controller.refreshContext();
    }
  }, [open, active?.id, active?.context, active?.contextLoading]);

  if (!active) return <PanelEmpty>{strings.sidebar.empty}</PanelEmpty>;

  const context = active.context;

  return (
    <>
      <div className="panel-toolbar">
        <button
          className="btn btn-sm"
          onClick={() => controller.refreshContext()}
          disabled={active.contextLoading}
        >
          <RefreshCw size={13} /> {strings.sidebar.refresh}
        </button>
      </div>

      {active.contextLoading ? <PanelLoading label={strings.sidebar.loading} /> : null}

      {!active.contextLoading && active.contextError ? (
        <PanelEmpty>
          {active.contextError.code}: {active.contextError.message}
        </PanelEmpty>
      ) : null}

      {!active.contextLoading && context ? (
        <div className="stack">
          <div className="stat-grid">
            <Stat label={strings.panel.digest} value={context.digest.slice(0, 16) || "—"} />
            <Stat
              label={strings.panel.estimatedTokens}
              value={formatTokens(context.estimated_tokens)}
            />
            <Stat
              label={strings.panel.messages}
              value={String(context.messages_total)}
            />
            <Stat label={strings.panel.tools} value={String(context.tools.length)} />
          </div>
          <div className="small muted">
            {strings.panel.source}: {context.source} · {strings.panel.kind}: {context.kind} ·{" "}
            {strings.panel.phase}: {context.phase} · {strings.panel.roundShort}{" "}
            {context.round}
          </div>
          {context.tools.length > 0 ? (
            <div className="panel-section">
              <div className="panel-section-title">{strings.sidebar.tools}</div>
              <div className="row" style={{ flexWrap: "wrap" }}>
                {context.tools.map((tool) => (
                  <span className="badge" key={tool}>
                    {tool}
                  </span>
                ))}
              </div>
            </div>
          ) : null}
          <div className="panel-section">
            <div className="panel-section-title">{strings.sidebar.digestShort}</div>
            <pre className="tool-pre">{context.digest || "—"}</pre>
          </div>
        </div>
      ) : null}

      {!active.contextLoading && !context && !active.contextError ? (
        <PanelEmpty>{strings.sidebar.empty}</PanelEmpty>
      ) : null}
    </>
  );
}
