import { useState } from "react";
import { useStrings } from "../../hooks/useStrings";
import { useAppState } from "../../hooks/useStore";
import { controller } from "../../transport/controller";
import { Dialog } from "../common/Dialog";
import { relativeTime } from "../common/format";

export function SessionPicker({ onClose }: { onClose: () => void }) {
  const strings = useStrings();
  const state = useAppState();
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();
  const sessions = state.catalog.items.filter((session) => {
    if (!needle) return true;
    return (
      (session.title ?? "").toLowerCase().includes(needle) ||
      session.session_id.toLowerCase().includes(needle) ||
      (session.workspace ?? "").toLowerCase().includes(needle)
    );
  });

  return (
    <Dialog title={strings.overlays.sessionPickerTitle} onClose={onClose} wide>
      <input
        className="input"
        placeholder={strings.overlays.filterSessions}
        value={query}
        autoFocus
        onChange={(event) => setQuery(event.target.value)}
      />
      {sessions.length === 0 ? (
        <div className="palette-empty">{strings.nav.empty}</div>
      ) : (
        <div className="mode-list">
          {sessions.map((session) => (
            <button
              key={session.session_id}
              className={`mode-item${state.active?.id === session.session_id ? " active" : ""}`}
              onClick={() => {
                controller.openSession(session.session_id, session);
                onClose();
              }}
            >
              <span className="mode-item-name">
                {session.title || session.session_id.slice(0, 18)}
                <span className="badge">{session.phase}</span>
              </span>
              <span className="mode-item-desc">
                {session.workspace ?? "—"} · {session.message_count}{" "}
                {strings.nav.messageCount} · {relativeTime(session.updated_at)}
              </span>
            </button>
          ))}
        </div>
      )}
    </Dialog>
  );
}
