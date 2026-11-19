import { PanelRightClose } from "lucide-react";
import { useStrings } from "../../hooks/useStrings";
import { store } from "../../hooks/useStore";
import { PanelHost } from "../sidebar/PanelHost";

export function RightRail() {
  const strings = useStrings();
  return (
    <>
      <div className="right-rail-header">
        <span className="right-rail-title">{strings.sidebar.title}</span>
        <button
          className="icon-btn"
          onClick={() => store.dispatch({ type: "ui/toggleRightRail" })}
          title={strings.sidebar.collapse}
          aria-label={strings.sidebar.collapse}
        >
          <PanelRightClose size={16} />
        </button>
      </div>
      <PanelHost />
    </>
  );
}
