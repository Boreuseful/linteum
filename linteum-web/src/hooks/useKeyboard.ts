import { useEffect } from "react";
import { store } from "./useStore";

/** Global accelerators. Mouse-first UI; these are shortcuts, not the only path. */
export function useKeyboard(): void {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      // The settings page owns its own keyboard handling (Escape goes back).
      if (store.getState().ui.page === "settings") return;
      const mod = event.ctrlKey || event.metaKey;
      const key = event.key.toLowerCase();

      if (mod && key === "k") {
        event.preventDefault();
        store.dispatch({
          type: "ui/openOverlay",
          overlay: { kind: "command-palette" },
        });
        return;
      }
      if (mod && key === "n") {
        event.preventDefault();
        store.dispatch({ type: "ui/openOverlay", overlay: { kind: "new-session" } });
        return;
      }
      if (event.key === "F1") {
        event.preventDefault();
        store.dispatch({ type: "ui/openOverlay", overlay: { kind: "help" } });
        return;
      }
      if (event.key === "F2") {
        event.preventDefault();
        store.dispatch({ type: "ui/openOverlay", overlay: { kind: "session-picker" } });
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
}
