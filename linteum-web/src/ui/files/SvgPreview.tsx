import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { useStrings } from "../../hooks/useStrings";
import { ZoomControls } from "./ZoomControls";
import { useZoomKeys } from "./useZoomKeys";
import {
  IDENTITY_VIEW,
  ZOOM_STEP,
  zoomViewBy,
  type ZoomView,
} from "./zoom";

interface DragState {
  pointerId: number;
  startX: number;
  startY: number;
  originX: number;
  originY: number;
}

/**
 * SVG preview with the same pan/zoom feel as the Mermaid viewer: drag to pan,
 * zoom keys and buttons around the centre, double-click to reset. The image is
 * scaled through its layout width so the browser re-rasterises the vector at
 * the new size instead of stretching a cached bitmap.
 */
export function SvgPreview({
  src,
  name,
  onError,
}: {
  src: string;
  name: string;
  onError: () => void;
}) {
  const strings = useStrings();
  const dragRef = useRef<DragState | null>(null);
  const [view, setView] = useState<ZoomView>(IDENTITY_VIEW);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    dragRef.current = null;
    setDragging(false);
    setView(IDENTITY_VIEW);
  }, [src]);

  const zoomIn = useCallback(
    () => setView((current) => zoomViewBy(current, ZOOM_STEP)),
    [],
  );
  const zoomOut = useCallback(
    () => setView((current) => zoomViewBy(current, 1 / ZOOM_STEP)),
    [],
  );
  const reset = useCallback(() => setView(IDENTITY_VIEW), []);
  useZoomKeys({ zoomIn, zoomOut, reset });

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    // Zoom buttons sit inside the stage; let them click without starting a pan.
    if ((event.target as HTMLElement).closest("button")) return;
    event.currentTarget.focus();
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

  return (
    <div
      className={`file-svg-stage${dragging ? " dragging" : ""}`}
      tabIndex={0}
      aria-label={name}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onDoubleClick={reset}
    >
      <div
        className="file-svg-canvas"
        style={{ transform: `translate(${view.x}px, ${view.y}px)` }}
      >
        <img
          src={src}
          alt={name}
          draggable={false}
          style={{ width: `${view.scale * 100}%` }}
          onError={onError}
        />
      </div>
      <ZoomControls
        scale={view.scale}
        onZoomIn={zoomIn}
        onZoomOut={zoomOut}
        onReset={reset}
      />
      <div className="file-zoom-hint">{strings.files.svgHint}</div>
    </div>
  );
}
