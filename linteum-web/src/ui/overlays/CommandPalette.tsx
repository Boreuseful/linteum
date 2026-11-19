import { useEffect, useMemo, useState } from "react";
import { useStrings } from "../../hooks/useStrings";
import { store } from "../../hooks/useStore";
import { controller } from "../../transport/controller";
import type { Strings } from "../../strings";
import { copyText } from "../common/format";

interface PaletteAction {
  id: string;
  label: string;
  shortcut?: string;
  run: () => void;
}

function buildActions(strings: Strings): PaletteAction[] {
  return [
    {
      id: "new-session",
      label: strings.commands.newSession,
      shortcut: "Ctrl/Cmd+N",
      run: () => store.dispatch({ type: "ui/openOverlay", overlay: { kind: "new-session" } }),
    },
    {
      id: "session-picker",
      label: strings.overlays.openSessionPicker,
      shortcut: "F2",
      run: () => store.dispatch({ type: "ui/openOverlay", overlay: { kind: "session-picker" } }),
    },
    {
      id: "toggle-left",
      label: strings.commands.toggleLeftRail,
      run: () => store.dispatch({ type: "ui/toggleLeftRail" }),
    },
    {
      id: "toggle-right",
      label: strings.commands.toggleRightRail,
      run: () => store.dispatch({ type: "ui/toggleRightRail" }),
    },
    {
      id: "refresh",
      label: strings.commands.refreshSessions,
      run: () => controller.refreshSessions(),
    },
    {
      id: "reconnect",
      label: strings.commands.reconnect,
      run: () => controller.connect(),
    },
    {
      id: "interrupt",
      label: strings.commands.interrupt,
      run: () => controller.interrupt(),
    },
    {
      id: "resume",
      label: strings.commands.resume,
      run: () => controller.resume(),
    },
    {
      id: "copy-last",
      label: strings.commands.copyLast,
      run: () => {
        const transcript = store.getState().active?.transcript ?? [];
        const last = [...transcript].reverse().find((item) => item.kind === "assistant");
        if (last && last.kind === "assistant") void copyText(last.text);
      },
    },
    {
      id: "last-artifact",
      label: strings.commands.openLastArtifact,
      run: () => {
        const transcript = store.getState().active?.transcript ?? [];
        const last = [...transcript]
          .reverse()
          .find((item) => item.kind === "tool" && item.outputRef);
        if (last && last.kind === "tool" && last.outputRef) {
          store.dispatch({
            type: "ui/openOverlay",
            overlay: { kind: "artifact", ref: last.outputRef, title: last.name },
          });
        }
      },
    },
    {
      id: "activity",
      label: strings.commands.showActivity,
      run: () => store.dispatch({ type: "ui/activityToggle" }),
    },
    {
      id: "modes",
      label: strings.commands.pickMode,
      run: () => store.dispatch({ type: "ui/openOverlay", overlay: { kind: "mode-picker" } }),
    },
    {
      id: "plugins",
      label: strings.commands.showPlugins,
      run: () => store.dispatch({ type: "ui/selectPanel", panelId: "plugins" }),
    },
    {
      id: "settings",
      label: strings.nav.settings,
      run: () => store.dispatch({ type: "ui/page", page: "settings" }),
    },
    {
      id: "help",
      label: strings.commands.showHelp,
      shortcut: "F1",
      run: () => store.dispatch({ type: "ui/openOverlay", overlay: { kind: "help" } }),
    },
    {
      id: "theme-dark",
      label: strings.commands.themeDark,
      run: () => store.dispatch({ type: "ui/theme", theme: "dark" }),
    },
    {
      id: "theme-light",
      label: strings.commands.themeLight,
      run: () => store.dispatch({ type: "ui/theme", theme: "light" }),
    },
    {
      id: "theme-mono",
      label: strings.commands.themeMono,
      run: () => store.dispatch({ type: "ui/theme", theme: "mono" }),
    },
    {
      id: "theme-system",
      label: strings.commands.themeSystem,
      run: () => store.dispatch({ type: "ui/theme", theme: "system" }),
    },
    {
      id: "leave",
      label: strings.commands.leaveSession,
      run: () => controller.leaveSession(),
    },
    {
      id: "disconnect",
      label: strings.commands.disconnect,
      run: () => controller.disconnect(),
    },
  ];
}

export function CommandPalette({ onClose }: { onClose: () => void }) {
  const strings = useStrings();
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const actions = useMemo(() => buildActions(strings), [strings]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return actions;
    return actions.filter((action) => action.label.toLowerCase().includes(needle));
  }, [actions, query]);

  useEffect(() => {
    setIndex(0);
  }, [query]);

  const run = (action: PaletteAction) => {
    onClose();
    action.run();
  };

  return (
    <div
      className="overlay-root"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="palette" role="dialog" aria-modal="true" aria-label={strings.overlays.commandPaletteTitle}>
        <input
          className="palette-input"
          autoFocus
          value={query}
          placeholder={strings.overlays.commandPalettePlaceholder}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              setIndex((value) => (filtered.length ? (value + 1) % filtered.length : 0));
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              setIndex((value) =>
                filtered.length ? (value - 1 + filtered.length) % filtered.length : 0,
              );
            } else if (event.key === "Enter") {
              event.preventDefault();
              const action = filtered[index];
              if (action) run(action);
            } else if (event.key === "Escape") {
              event.preventDefault();
              onClose();
            }
          }}
        />
        <div className="palette-list">
          {filtered.length === 0 ? (
            <div className="palette-empty">{strings.overlays.noMatchingCommand}</div>
          ) : (
            filtered.map((action, actionIndex) => (
              <button
                key={action.id}
                className={`palette-item${actionIndex === index ? " active" : ""}`}
                onMouseEnter={() => setIndex(actionIndex)}
                onMouseDown={(event) => {
                  event.preventDefault();
                  run(action);
                }}
              >
                <span>{action.label}</span>
                {action.shortcut ? (
                  <span className="kbd palette-shortcut">{action.shortcut}</span>
                ) : null}
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
