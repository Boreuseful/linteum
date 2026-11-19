import { X } from "lucide-react";
import { useEffect, type ReactNode } from "react";
import { useStrings } from "../../hooks/useStrings";

export interface DialogProps {
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  headerExtra?: ReactNode;
  onClose: () => void;
  wide?: boolean;
}

export function Dialog({
  title,
  children,
  footer,
  headerExtra,
  onClose,
  wide = false,
}: DialogProps) {
  const strings = useStrings();
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

  return (
    <div
      className="overlay-root"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className={`dialog${wide ? " wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="dialog-head">
          <h2>{title}</h2>
          {headerExtra}
          <button className="icon-btn" onClick={onClose} aria-label={strings.overlays.close}>
            <X size={16} />
          </button>
        </div>
        <div className="dialog-body">{children}</div>
        {footer ? <div className="dialog-foot">{footer}</div> : null}
      </div>
    </div>
  );
}
