import { X } from "lucide-react";
import { useStrings } from "../../hooks/useStrings";
import { store, useAppState } from "../../hooks/useStore";
import { formatClock } from "../common/format";

export function ActivityDrawer() {
  const strings = useStrings();
  const state = useAppState();
  return (
    <aside className="activity-drawer" aria-label={strings.nav.activity}>
      <div className="activity-head">
        <h2>{strings.nav.activity}</h2>
        <button
          className="btn btn-sm"
          onClick={() => store.dispatch({ type: "ui/activityClear" })}
        >
          {strings.activity.clear}
        </button>
        <button
          className="icon-btn"
          onClick={() => store.dispatch({ type: "ui/activityToggle" })}
          aria-label={strings.activity.close}
        >
          <X size={16} />
        </button>
      </div>
      <div className="activity-list">
        {state.ui.activity.length === 0 ? (
          <div className="panel-empty">{strings.activity.empty}</div>
        ) : (
          [...state.ui.activity].reverse().map((entry) => (
            <div key={entry.id} className={`activity-entry ${entry.kind}`}>
              <span className="activity-time">{formatClock(entry.ts)}</span>
              <span>{entry.message}</span>
            </div>
          ))
        )}
      </div>
    </aside>
  );
}
