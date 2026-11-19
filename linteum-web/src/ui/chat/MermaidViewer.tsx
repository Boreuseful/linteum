import { RotateCcw, X, ZoomIn, ZoomOut } from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { createPortal } from "react-dom";
import { useStrings } from "../../hooks/useStrings";
import { svgAspect } from "./mermaidExport";

const MIN_SCALE = 0.2;
const MAX_SCALE = 8;
/** Leaves room for the header and a margin around the diagram. */
const FIT_WIDTH = 92;
const FIT_HEIGHT = 84;

interface ViewState {
  scale: number;
  x: number;
  y: number;
}

interface DragState {
  pointerId: number;
  startX: number;
  startY: number;
  originX: number;
  originY: number;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/**
 * Full-viewport Mermaid viewer: wheel zooms around the cursor, dragging pans,
 * and the header exposes the same operations for keyboard/mouse users.
 */
export function MermaidViewer({ svg, onClose }: { svg: string; onClose: () => void }) {
  const strings = useStrings();
  const stageRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const [view, setView] = useState<ViewState>({ scale: 1, x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const aspect = svgAspect(svg);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, []);

  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;

    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      event.stopPropagation();
      const rect = stage.getBoundingClientRect();
      const cursorX = event.clientX - rect.left - rect.width / 2;
      const cursorY = event.clientY - rect.top - rect.height / 2;
      const delta =
        event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? rect.height : 1);

      setView((current) => {
        const scale = clamp(
          current.scale * Math.exp(-delta * 0.0015),
          MIN_SCALE,
          MAX_SCALE,
        );
        const ratio = scale / current.scale;
        return {
          scale,
          x: cursorX - (cursorX - current.x) * ratio,
          y: cursorY - (cursorY - current.y) * ratio,
        };
      });
    };

    stage.addEventListener("wheel", onWheel, { passive: false });
    return () => stage.removeEventListener("wheel", onWheel);
  }, []);

  const zoomBy = (factor: number) => {
    setView((current) => {
      const scale = clamp(current.scale * factor, MIN_SCALE, MAX_SCALE);
      const ratio = scale / current.scale;
      return { scale, x: current.x * ratio, y: current.y * ratio };
    });
  };

  const reset = () => setView({ scale: 1, x: 0, y: 0 });

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: view.x,
      originY: view.y,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging(true);
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    setView((current) => ({
      ...current,
      x: drag.originX + event.clientX - drag.startX,
      y: drag.originY + event.clientY - drag.startY,
    }));
  };

  const endDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId !== event.pointerId) return;
    dragRef.current = null;
    setDragging(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  if (typeof document === "undefined") return null;

  return createPortal(
    <div
      className="mermaid-viewer"
      role="dialog"
      aria-modal="true"
      aria-label={strings.chat.viewerTitle}
    >
      <div className="mermaid-viewer-head">
        <span className="mermaid-viewer-title">{strings.chat.viewerTitle}</span>
        <span className="mermaid-viewer-hint">{strings.chat.viewerHint}</span>
        <div className="mermaid-viewer-controls">
          <button
            className="icon-btn"
            onClick={() => zoomBy(1 / 1.25)}
            title={strings.chat.zoomOut}
            aria-label={strings.chat.zoomOut}
          >
            <ZoomOut size={16} />
          </button>
          <span className="mermaid-viewer-scale">{Math.round(view.scale * 100)}%</span>
          <button
            className="icon-btn"
            onClick={() => zoomBy(1.25)}
            title={strings.chat.zoomIn}
            aria-label={strings.chat.zoomIn}
          >
            <ZoomIn size={16} />
          </button>
          <button
            className="icon-btn"
            onClick={reset}
            title={strings.chat.resetView}
            aria-label={strings.chat.resetView}
          >
            <RotateCcw size={16} />
          </button>
          <button
            className="icon-btn"
            onClick={onClose}
            title={strings.chat.closeViewer}
            aria-label={strings.chat.closeViewer}
            autoFocus
          >
            <X size={16} />
          </button>
        </div>
      </div>
      <div
        ref={stageRef}
        className={`mermaid-viewer-stage${dragging ? " dragging" : ""}`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onDoubleClick={reset}
      >
        <div
          className="mermaid-viewer-canvas"
          style={{
            // Scale through layout width, not `transform: scale()`: the SVG is
            // re-rendered as vectors at every zoom level instead of stretching
            // a cached raster of the composited layer.
            width: `min(${(FIT_WIDTH * view.scale).toFixed(2)}vw, ${(
              FIT_HEIGHT * aspect * view.scale
            ).toFixed(2)}vh)`,
            transform: `translate(${view.x}px, ${view.y}px)`,
          }}
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      </div>
    </div>,
    document.body,
  );
}
