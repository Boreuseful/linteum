import {
  Activity,
  Command,
  HelpCircle,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRight,
} from "lucide-react";
import { useStrings } from "../../hooks/useStrings";
import { store, useAppState } from "../../hooks/useStore";
import type { ThemeChoice } from "../../state/types";
import { formatTokens, modeLabel } from "../common/format";
import { ThemeIcon } from "../common/ThemeIcon";

function nextTheme(theme: ThemeChoice): ThemeChoice {
  if (theme === "dark") return "light";
  if (theme === "light") return "mono";
  if (theme === "mono") return "system";
  return "dark";
}

export function TopBar() {
  const strings = useStrings();
  const state = useAppState();
  const { connection, ui, active } = state;
  const totalTokens =
    (active?.usage.input_tokens ?? 0) + (active?.usage.output_tokens ?? 0);

  return (
    <header className="topbar">
      <button
        className={`icon-btn${ui.leftRailOpen ? " active" : ""}`}
        onClick={() => store.dispatch({ type: "ui/toggleLeftRail" })}
        title={ui.leftRailOpen ? strings.sidebar.collapse : strings.sidebar.expand}
        aria-label={ui.leftRailOpen ? strings.sidebar.collapse : strings.sidebar.expand}
        aria-expanded={ui.leftRailOpen}
      >
        {ui.leftRailOpen ? <PanelLeftClose size={17} /> : <PanelLeftOpen size={17} />}
      </button>

      <div className="topbar-status" title={connection.url}>
        <span className={`status-dot ${connection.status}`} />
        <span className="truncate">{strings.status[connection.status]}</span>
      </div>

      <div className="topbar-meta">
        {active?.boundModel ? <span className="badge">{active.boundModel}</span> : null}
        {active?.mode ? (
          <button
            className="badge badge-accent"
            onClick={() =>
              store.dispatch({ type: "ui/openOverlay", overlay: { kind: "mode-picker" } })
            }
            title={strings.commands.pickMode}
          >
            {modeLabel(active.mode)}
          </button>
        ) : null}
        {active ? (
          <span className="badge">
            {strings.topbar.round} {active.round}
          </span>
        ) : null}
        {active && totalTokens > 0 ? (
          <span className="badge">
            {formatTokens(totalTokens)} {strings.topbar.tokens}
          </span>
        ) : null}
      </div>

      <div className="topbar-spacer" />

      <div className="topbar-actions">
        <button
          className={`icon-btn${ui.rightRailOpen ? " active" : ""}`}
          onClick={() => store.dispatch({ type: "ui/toggleRightRail" })}
          disabled={!active}
          title={strings.sidebar.title}
          aria-label={strings.sidebar.title}
        >
          <PanelRight size={17} />
        </button>
        <button
          className="icon-btn"
          onClick={() =>
            store.dispatch({ type: "ui/openOverlay", overlay: { kind: "command-palette" } })
          }
          title={strings.overlays.commandPaletteShortcut}
          aria-label={strings.topbar.commandPalette}
        >
          <Command size={17} />
        </button>
        <button
          className="icon-btn"
          onClick={() => store.dispatch({ type: "ui/theme", theme: nextTheme(ui.theme) })}
          title={strings.topbar.changeTheme}
          aria-label={strings.topbar.changeTheme}
        >
          <ThemeIcon theme={ui.theme} size={17} />
        </button>
        <button
          className={`icon-btn${ui.activityOpen ? " active" : ""}`}
          onClick={() => store.dispatch({ type: "ui/activityToggle" })}
          title={strings.nav.activity}
          aria-label={strings.nav.activity}
        >
          <Activity size={17} />
        </button>
        <button
          className="icon-btn"
          onClick={() => store.dispatch({ type: "ui/openOverlay", overlay: { kind: "help" } })}
          title={strings.nav.help}
          aria-label={strings.nav.help}
        >
          <HelpCircle size={17} />
        </button>
      </div>
    </header>
  );
}
