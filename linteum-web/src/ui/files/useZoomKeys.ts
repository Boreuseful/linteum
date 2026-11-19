import { useEffect, useRef } from "react";
import { isEditableTarget, zoomCommandForKey } from "./zoom";

interface ZoomHandlers {
  zoomIn: () => void;
  zoomOut: () => void;
  reset: () => void;
}

/**
 * Window-level zoom keys for the active file preview. `+`/`=` zoom in,
 * `-`/`_` zoom out and `0` resets. Typing targets are ignored so the chat
 * composer and the explorer filter keep their keys.
 */
export function useZoomKeys(handlers: ZoomHandlers): void {
  const current = useRef(handlers);
  current.current = handlers;

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      // Leave browser and OS accelerators alone; Shift is allowed because
      // most layouts need it for "+".
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (isEditableTarget(event.target)) return;
      const command = zoomCommandForKey(event.key);
      if (!command) return;
      event.preventDefault();
      if (command === "in") current.current.zoomIn();
      else if (command === "out") current.current.zoomOut();
      else current.current.reset();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
}
