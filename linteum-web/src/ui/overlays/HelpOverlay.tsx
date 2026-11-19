import { useStrings } from "../../hooks/useStrings";
import type { Strings } from "../../strings";
import { Dialog } from "../common/Dialog";

function rows(strings: Strings): Array<[string, string]> {
  return [
    ["Ctrl/Cmd+K", strings.overlays.helpCommandPalette],
    ["Ctrl/Cmd+N", strings.overlays.helpNewSession],
    ["F2", strings.overlays.helpSessionPicker],
    ["F1", strings.overlays.helpThisHelp],
    ["Enter", strings.overlays.helpSendMessage],
    ["Shift+Enter", strings.overlays.helpNewLine],
    ["/", strings.overlays.helpSlashCommands],
    ["y / n", strings.overlays.helpApproveDeny],
    ["Esc", strings.overlays.helpCloseDialog],
    ["Click a tool card", strings.overlays.helpExpandTool],
  ];
}

export function HelpOverlay({ onClose }: { onClose: () => void }) {
  const strings = useStrings();
  return (
    <Dialog title={strings.overlays.helpTitle} onClose={onClose}>
      <div className="help-grid">
        {rows(strings).map(([key, label]) => (
          <div className="help-row" key={key}>
            <span className="kbd">{key}</span>
            <span>{label}</span>
          </div>
        ))}
      </div>
      <div className="small muted">{strings.overlays.helpFootnote}</div>
    </Dialog>
  );
}
