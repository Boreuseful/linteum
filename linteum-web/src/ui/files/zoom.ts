/** Zoom bounds shared by the SVG and PDF previews. */
export const MIN_ZOOM = 0.25;
export const MAX_ZOOM = 8;
export const ZOOM_STEP = 1.25;

export interface ZoomView {
  scale: number;
  /** Pan offset in pixels, applied after scaling. */
  x: number;
  y: number;
}

export const IDENTITY_VIEW: ZoomView = { scale: 1, x: 0, y: 0 };

export function clampZoom(scale: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, scale));
}

/**
 * Zoom around the viewport centre. The pan offset grows with the same ratio
 * so the point under the centre stays put, matching the Mermaid viewer.
 */
export function zoomViewBy(view: ZoomView, factor: number): ZoomView {
  const scale = clampZoom(view.scale * factor);
  const ratio = scale / view.scale;
  return { scale, x: view.x * ratio, y: view.y * ratio };
}

export type ZoomCommand = "in" | "out" | "reset";

/** `+`/`=` zoom in, `-`/`_` zoom out, `0` resets. */
export function zoomCommandForKey(key: string): ZoomCommand | null {
  switch (key) {
    case "+":
    case "=":
      return "in";
    case "-":
    case "_":
      return "out";
    case "0":
      return "reset";
    default:
      return null;
  }
}

/** Keyboard zoom must never fire while the user is typing. */
export function isEditableTarget(target: EventTarget | null): boolean {
  if (!target || typeof target !== "object") return false;
  const element = target as { tagName?: unknown; isContentEditable?: unknown };
  if (element.isContentEditable === true) return true;
  return (
    element.tagName === "INPUT" ||
    element.tagName === "TEXTAREA" ||
    element.tagName === "SELECT"
  );
}
