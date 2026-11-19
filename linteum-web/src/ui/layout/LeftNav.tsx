import { Plus, RefreshCw, Settings } from "lucide-react";
import { useStrings } from "../../hooks/useStrings";
import { store, useAppState } from "../../hooks/useStore";
import { controller } from "../../transport/controller";
import { PanelLoading } from "../common/EmptyState";
import { relativeTime } from "../common/format";

export function LeftNav() {
  const strings = useStrings();
  const state = useAppState();
  const sessions = state.catalog.items;

  const open = (sessionId: string, info: (typeof sessions)[number]) => {
    controller.openSession(sessionId, info);
    if (window.innerWidth < 960) {
      store.dispatch({ type: "ui/toggleLeftRail" });
    }
  };

  return (
    <>
      <div className="rail-header">
        <span className="rail-title">{strings.nav.sessions}</span>
        <button
          className="icon-btn"
          onClick={() => controller.refreshSessions()}
          title={strings.nav.refresh}
          aria-label={strings.nav.refresh}
        >
          <RefreshCw size={15} />
        </button>
      </div>

      <button
        className="btn btn-primary rail-new"
        onClick={() => store.dispatch({ type: "ui/openOverlay", overlay: { kind: "new-session" } })}
      >
        <Plus size={15} /> {strings.nav.newSession}
      </button>

      <div className="session-list">
        {state.catalog.loading && sessions.length === 0 ? (
          <PanelLoading label={strings.sidebar.loading} />
        ) : null}

        {!state.catalog.loading && state.catalog.error ? (
          <div className="panel-empty">
            {state.catalog.error.code}: {state.catalog.error.message}
          </div>
        ) : null}

        {!state.catalog.loading && !state.catalog.error && sessions.length === 0 ? (
          <div className="panel-empty">
            <div>{strings.nav.empty}</div>
            <div className="small" style={{ marginTop: 4 }}>
              {strings.nav.emptyHint}
            </div>
          </div>
        ) : null}

        {sessions.map((session) => (
          <button
            key={session.session_id}
            className={`session-item${state.active?.id === session.session_id ? " active" : ""}`}
            onClick={() => open(session.session_id, session)}
          >
            <div className="session-item-title">
              <span
                className={`session-phase ${session.phase === "idle" ? "idle" : "busy"}`}
                aria-hidden="true"
              />
              <span>{session.title || session.session_id.slice(0, 14)}</span>
            </div>
            <div className="session-item-meta">
              <span>{session.workspace ?? "—"}</span>
              <span>·</span>
              <span>{relativeTime(session.updated_at)}</span>
              <span>·</span>
              <span>
                {session.message_count} {strings.nav.messageCount}
              </span>
            </div>
          </button>
        ))}
      </div>

      <div className="rail-footer">
        <span className={`status-dot ${state.connection.status}`} />
        <span className="truncate" title={state.connection.url}>
          {state.connection.instanceId
            ? `${strings.nav.instance} ${state.connection.instanceId.slice(0, 8)}`
            : state.connection.url}
        </span>
      </div>

      <button
        className="rail-settings"
        onClick={() => store.dispatch({ type: "ui/page", page: "settings" })}
      >
        <Settings size={15} />
        <span>{strings.nav.settings}</span>
      </button>
    </>
  );
}
