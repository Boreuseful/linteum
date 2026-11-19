import { useStrings } from "../../hooks/useStrings";
import { store } from "../../hooks/useStore";
import { controller } from "../../transport/controller";
import type { Strings } from "../../strings";

export interface SlashCommand {
  command: string;
  label: string;
  run: () => void;
}

export function slashCommands(strings: Strings): SlashCommand[] {
  return [
    {
      command: "/new",
      label: strings.slash.newSession,
      run: () => store.dispatch({ type: "ui/openOverlay", overlay: { kind: "new-session" } }),
    },
    {
      command: "/files",
      label: strings.slash.openWorkspace,
      run: () => store.dispatch({ type: "ui/selectPanel", panelId: "workspace" }),
    },
    {
      command: "/sidebar",
      label: strings.commands.toggleRightRail,
      run: () => store.dispatch({ type: "ui/toggleRightRail" }),
    },
    {
      command: "/preview",
      label: strings.slash.workspacePreview,
      run: () => store.dispatch({ type: "ui/selectPanel", panelId: "workspace" }),
    },
    {
      command: "/info",
      label: strings.slash.infoPanel,
      run: () => store.dispatch({ type: "ui/selectPanel", panelId: "info" }),
    },
    {
      command: "/subagents",
      label: strings.slash.subagentsPanel,
      run: () => store.dispatch({ type: "ui/selectPanel", panelId: "subagents" }),
    },
    {
      command: "/plugins",
      label: strings.slash.pluginsPanel,
      run: () => store.dispatch({ type: "ui/selectPanel", panelId: "plugins" }),
    },
    {
      command: "/refresh",
      label: strings.slash.refreshSessions,
      run: () => controller.refreshSessions(),
    },
    {
      command: "/merge",
      label: strings.slash.stackPanels,
      run: () => store.dispatch({ type: "ui/panelMode", mode: "stack" }),
    },
    {
      command: "/split",
      label: strings.slash.tabPanels,
      run: () => store.dispatch({ type: "ui/panelMode", mode: "tabs" }),
    },
    {
      command: "/collapse",
      label: strings.slash.collapsePanel,
      run: () => {
        const panelId = store.getState().ui.activePanelId;
        if (panelId) store.dispatch({ type: "ui/togglePanel", panelId });
      },
    },
    {
      command: "/expand",
      label: strings.slash.expandPanel,
      run: () => {
        const panelId = store.getState().ui.activePanelId;
        if (panelId) store.dispatch({ type: "ui/selectPanel", panelId });
      },
    },
    {
      command: "/stop",
      label: strings.slash.stopAgent,
      run: () => controller.interrupt(),
    },
    {
      command: "/leave",
      label: strings.slash.leaveSession,
      run: () => controller.leaveSession(),
    },
    {
      command: "/help",
      label: strings.slash.help,
      run: () => store.dispatch({ type: "ui/openOverlay", overlay: { kind: "help" } }),
    },
    {
      command: "/quit",
      label: strings.slash.disconnect,
      run: () => controller.disconnect(),
    },
  ];
}

export function SlashMenu({
  commands,
  activeIndex,
  onSelect,
  onHover,
}: {
  commands: SlashCommand[];
  activeIndex: number;
  onSelect: (command: SlashCommand) => void;
  onHover: (index: number) => void;
}) {
  const strings = useStrings();
  if (commands.length === 0) return null;
  return (
    <div
      className="slash-menu"
      role="listbox"
      aria-label={strings.overlays.helpSlashCommands}
    >
      <div className="slash-menu-title">{strings.slash.title}</div>
      {commands.map((command, index) => (
        <button
          key={command.command}
          role="option"
          aria-selected={index === activeIndex}
          className={`slash-item${index === activeIndex ? " active" : ""}`}
          onMouseEnter={() => onHover(index)}
          onMouseDown={(event) => {
            event.preventDefault();
            onSelect(command);
          }}
        >
          <code>{command.command}</code>
          <span>{command.label}</span>
        </button>
      ))}
    </div>
  );
}
