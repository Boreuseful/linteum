import { RotateCcw, ZoomIn, ZoomOut } from "lucide-react";
import { useStrings } from "../../hooks/useStrings";

interface ZoomControlsProps {
  scale: number;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onReset: () => void;
}

/** Floating zoom controls shared by the SVG and PDF previews. */
export function ZoomControls({
  scale,
  onZoomIn,
  onZoomOut,
  onReset,
}: ZoomControlsProps) {
  const strings = useStrings();
  return (
    <div
      className="file-zoom-controls"
      onPointerDown={(event) => event.stopPropagation()}
      onDoubleClick={(event) => event.stopPropagation()}
    >
      <button
        className="icon-btn"
        onClick={onZoomOut}
        title={strings.chat.zoomOut}
        aria-label={strings.chat.zoomOut}
      >
        <ZoomOut size={14} />
      </button>
      <span className="file-zoom-scale">{Math.round(scale * 100)}%</span>
      <button
        className="icon-btn"
        onClick={onZoomIn}
        title={strings.chat.zoomIn}
        aria-label={strings.chat.zoomIn}
      >
        <ZoomIn size={14} />
      </button>
      <button
        className="icon-btn"
        onClick={onReset}
        title={strings.chat.resetView}
        aria-label={strings.chat.resetView}
      >
        <RotateCcw size={14} />
      </button>
    </div>
  );
}
