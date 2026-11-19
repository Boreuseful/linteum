import { ChevronDown, ChevronRight } from "lucide-react";
import { useStrings } from "../../hooks/useStrings";
import { store, useAppState } from "../../hooks/useStore";
import { ErrorBoundary } from "../common/ErrorBoundary";
import type { SidebarPanel } from "./PanelRegistry";
import { availablePanels } from "./PanelRegistry";

function panelCount(panel: SidebarPanel, state: ReturnType<typeof useAppState>): string {
  const active = state.active;
  if (!active) return "";
  if (panel.id === "subagents") return String(Object.keys(active.subagents).length);
  if (panel.id === "background") return String(Object.keys(active.background).length);
  if (panel.id === "plugins") return String(state.plugins.items.length);
  return "";
}

/**
 * Right-sidebar container. Switching between stack/tabs/drawer is a change to
 * this file only; individual panels implement their own content.
 */
export function PanelHost() {
  const strings = useStrings();
  const state = useAppState();
  const panels = availablePanels();

  if (!state.active) {
    return <div className="panel-empty">{strings.sidebar.selectSession}</div>;
  }

  if (state.ui.panelMode === "tabs") {
    const activeId =
      state.ui.activePanelId && panels.some((panel) => panel.id === state.ui.activePanelId)
        ? state.ui.activePanelId
        : panels[0]?.id;
    const current = panels.find((panel) => panel.id === activeId);
    const CurrentPanel = current?.Component;
    return (
      <>
        <div className="panel-tabs" role="tablist">
          {panels.map((panel) => {
            const Icon = panel.icon;
            return (
              <button
                key={panel.id}
                role="tab"
                aria-selected={panel.id === activeId}
                className={`panel-tab${panel.id === activeId ? " active" : ""}`}
                onClick={() => store.dispatch({ type: "ui/selectPanel", panelId: panel.id })}
              >
                <Icon size={15} />
                <span>{panel.title(strings)}</span>
              </button>
            );
          })}
        </div>
        {CurrentPanel ? (
          <div className="panel-body flush">
            <ErrorBoundary key={state.active.id} label={current?.title(strings)}>
              <CurrentPanel key={state.active.id} sessionId={state.active.id} />
            </ErrorBoundary>
          </div>
        ) : (
          <div className="panel-empty">{strings.sidebar.noPanels}</div>
        )}
      </>
    );
  }

  return (
    <div>
      {panels.map((panel) => {
        const open = state.ui.openPanels[panel.id] ?? panel.defaultOpen ?? false;
        const count = panelCount(panel, state);
        const Component = panel.Component;
        const Icon = panel.icon;
        return (
          <section className="panel" key={panel.id}>
            <button
              className="panel-head"
              aria-expanded={open}
              onClick={() => store.dispatch({ type: "ui/togglePanel", panelId: panel.id })}
            >
              <span className="panel-head-icon">
                <Icon size={15} />
              </span>
              <span className="panel-head-title">{panel.title(strings)}</span>
              {count ? <span className="panel-head-count">{count}</span> : null}
              {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            </button>
            {open ? (
              <div className="panel-body">
                <ErrorBoundary
                  key={state.active?.id ?? "none"}
                  label={panel.title(strings)}
                >
                  <Component
                    key={state.active?.id ?? "none"}
                    sessionId={state.active?.id ?? null}
                  />
                </ErrorBoundary>
              </div>
            ) : null}
          </section>
        );
      })}
    </div>
  );
}
