import { RefreshCw } from "lucide-react";
import { useEffect } from "react";
import { useStrings } from "../../hooks/useStrings";
import { useAppState } from "../../hooks/useStore";
import { controller } from "../../transport/controller";
import { Dialog } from "../common/Dialog";
import { PanelLoading } from "../common/EmptyState";

export function ModePicker({ onClose }: { onClose: () => void }) {
  const strings = useStrings();
  const state = useAppState();
  const active = state.active;
  const modes = active?.modes ?? [];

  useEffect(() => {
    if (active && modes.length === 0 && !active.modesLoading) {
      controller.refreshModes();
    }
  }, [active?.id]);

  return (
    <Dialog title={strings.overlays.modePickerTitle} onClose={onClose}>
      {active?.modesLoading ? <PanelLoading label={strings.sidebar.loading} /> : null}
      {!active?.modesLoading && modes.length === 0 ? (
        <div className="palette-empty">{strings.overlays.noModes}</div>
      ) : null}
      <div className="mode-list">
        {modes.map((mode) => (
          <button
            key={mode.id}
            className={`mode-item${active?.mode?.id === mode.id ? " active" : ""}`}
            onClick={() => {
              // `session.mode.set` takes the mode name; the id
              // (`<source>/<name>`) is rejected as an invalid mode name.
              controller.setMode(mode.name);
              onClose();
            }}
          >
            <span className="mode-item-name">
              {mode.name}
              {mode.default ? (
                <span className="badge">{strings.overlays.defaultBadge}</span>
              ) : null}
              {mode.builtin ? (
                <span className="badge">{strings.overlays.builtinBadge}</span>
              ) : null}
            </span>
            {mode.description ? (
              <span className="mode-item-desc">{mode.description}</span>
            ) : null}
          </button>
        ))}
      </div>
      <div>
        <button className="btn btn-sm" onClick={() => controller.refreshModes()}>
          <RefreshCw size={13} /> {strings.sidebar.refresh}
        </button>
      </div>
    </Dialog>
  );
}
