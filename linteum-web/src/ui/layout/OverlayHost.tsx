import { store, useAppState } from "../../hooks/useStore";
import { ApprovalDialog } from "../overlays/ApprovalDialog";
import { ArtifactViewer } from "../overlays/ArtifactViewer";
import { ChildContextDialog } from "../overlays/ChildContextDialog";
import { CommandPalette } from "../overlays/CommandPalette";
import { HelpOverlay } from "../overlays/HelpOverlay";
import { ModePicker } from "../overlays/ModePicker";
import { NewSessionDialog } from "../overlays/NewSessionDialog";
import { PluginDetailDialog } from "../overlays/PluginDetailDialog";
import { SessionPicker } from "../overlays/SessionPicker";

export function OverlayHost() {
  const state = useAppState();
  const close = () => store.dispatch({ type: "ui/closeOverlay" });

  // Approvals take precedence over any open dialog: they are blocking the turn.
  // While history hydrates, approvals are still part of the buffered replay, so
  // never mount the dialog for them.
  if (!state.active?.hydrating && (state.active?.approvals.length ?? 0) > 0) {
    return <ApprovalDialog />;
  }

  const overlay = state.ui.overlays[state.ui.overlays.length - 1];
  if (!overlay) return null;

  switch (overlay.kind) {
    case "new-session":
      return <NewSessionDialog onClose={close} />;
    case "session-picker":
      return <SessionPicker onClose={close} />;
    case "command-palette":
      return <CommandPalette onClose={close} />;
    case "help":
      return <HelpOverlay onClose={close} />;
    case "mode-picker":
      return <ModePicker onClose={close} />;
    case "plugin-detail":
      return <PluginDetailDialog pluginId={overlay.pluginId} onClose={close} />;
    case "child-context":
      return (
        <ChildContextDialog
          childId={overlay.childId}
          title={overlay.title}
          onClose={close}
        />
      );
    case "artifact":
      return (
        <ArtifactViewer artifactRef={overlay.ref} title={overlay.title} onClose={close} />
      );
    default:
      return null;
  }
}
