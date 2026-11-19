import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  DEFAULT_FILE_PANE_WIDTH,
  DEFAULT_LEFT_RAIL_WIDTH,
  DEFAULT_RIGHT_RAIL_WIDTH,
  MAX_FILE_PANE_WIDTH,
  MAX_LEFT_RAIL_WIDTH,
  MAX_RIGHT_RAIL_WIDTH,
  MIN_FILE_PANE_WIDTH,
  MIN_LEFT_RAIL_WIDTH,
  MIN_RIGHT_RAIL_WIDTH,
} from "../../config";
import { useStrings } from "../../hooks/useStrings";
import { store, useAppState } from "../../hooks/useStore";

export type RailKind = "left" | "right" | "file";

const LIMITS: Record<RailKind, { min: number; max: number; fallback: number }> = {
  left: {
    min: MIN_LEFT_RAIL_WIDTH,
    max: MAX_LEFT_RAIL_WIDTH,
    fallback: DEFAULT_LEFT_RAIL_WIDTH,
  },
  right: {
    min: MIN_RIGHT_RAIL_WIDTH,
    max: MAX_RIGHT_RAIL_WIDTH,
    fallback: DEFAULT_RIGHT_RAIL_WIDTH,
  },
  file: {
    min: MIN_FILE_PANE_WIDTH,
    max: MAX_FILE_PANE_WIDTH,
    fallback: DEFAULT_FILE_PANE_WIDTH,
  },
};

/** Leave at least this much room for the transcript on desktop. */
const MIN_MAIN_WIDTH = 360;
const KEYBOARD_STEP = 16;

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/**
 * Drag handle on a rail's inner edge. Double-click resets the rail to its
 * default width; arrow keys move it in 16px steps.
 */
export function RailResizer({ kind }: { kind: RailKind }) {
  const strings = useStrings();
  const state = useAppState();
  const [dragging, setDragging] = useState(false);
  const cleanupRef = useRef<(() => void) | null>(null);

  const limits = LIMITS[kind];
  const width =
    kind === "left"
      ? state.ui.leftRailWidth
      : kind === "right"
        ? state.ui.rightRailWidth
        : state.ui.filePaneWidth;
  // Every other pane competes for the same viewport width.
  const reserved =
    kind === "left"
      ? (state.ui.rightRailOpen ? state.ui.rightRailWidth : 0) +
        (state.ui.filePaneOpen ? state.ui.filePaneWidth : 0)
      : kind === "right"
        ? state.ui.leftRailWidth +
          (state.ui.filePaneOpen ? state.ui.filePaneWidth : 0)
        : state.ui.leftRailWidth +
          (state.ui.rightRailOpen ? state.ui.rightRailWidth : 0);

  const maxForViewport = (): number =>
    Math.max(
      limits.min,
      Math.min(limits.max, window.innerWidth - reserved - MIN_MAIN_WIDTH),
    );

  const applyWidth = (value: number, commit: boolean) => {
    if (kind === "file") {
      store.dispatch({ type: "ui/filePaneWidth", width: value, commit });
      return;
    }
    store.dispatch({ type: "ui/railWidth", side: kind, width: value, commit });
  };

  useEffect(
    () => () => {
      cleanupRef.current?.();
    },
    [],
  );

  const beginDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Pointer capture is best-effort; window listeners still drive the drag.
    }

    const startX = event.clientX;
    const startWidth = width;
    let latest = startWidth;

    setDragging(true);
    document.body.classList.add("rail-resizing");

    const onMove = (move: PointerEvent) => {
      const delta = kind === "left" ? move.clientX - startX : startX - move.clientX;
      latest = clamp(startWidth + delta, limits.min, maxForViewport());
      applyWidth(latest, false);
    };

    const finish = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
      document.body.classList.remove("rail-resizing");
      cleanupRef.current = null;
      setDragging(false);
      applyWidth(latest, true);
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
    cleanupRef.current = finish;
  };

  const reset = () => {
    applyWidth(limits.fallback, true);
  };

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const direction = event.key === "ArrowRight" ? 1 : -1;
    const delta = kind === "left" ? direction * KEYBOARD_STEP : -direction * KEYBOARD_STEP;
    applyWidth(clamp(width + delta, limits.min, maxForViewport()), true);
  };

  const label =
    kind === "left"
      ? strings.sidebar.resizeSessions
      : kind === "right"
        ? strings.sidebar.resizeContext
        : strings.sidebar.resizeFiles;

  return (
    <div
      className={`rail-resizer ${kind === "left" ? "left" : "right"}${dragging ? " dragging" : ""}`}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuemin={limits.min}
      aria-valuemax={limits.max}
      aria-valuenow={width}
      title={`${label} — ${strings.sidebar.resizeHint}`}
      tabIndex={0}
      onPointerDown={beginDrag}
      onKeyDown={onKeyDown}
      onDoubleClick={(event) => {
        event.stopPropagation();
        reset();
      }}
    />
  );
}
