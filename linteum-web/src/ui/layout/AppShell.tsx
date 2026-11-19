import type {
  CSSProperties,
  MouseEvent as ReactMouseEvent,
  ReactNode,
} from "react";
import {
  DEFAULT_FILE_PANE_WIDTH,
  DEFAULT_LEFT_RAIL_WIDTH,
  DEFAULT_RIGHT_RAIL_WIDTH,
} from "../../config";
import { useStrings } from "../../hooks/useStrings";
import { store } from "../../hooks/useStore";
import type { FilePanePosition } from "../../state/types";
import { RailResizer, type RailKind } from "./RailResizer";

export interface AppShellProps {
  topBar: ReactNode;
  leftNav: ReactNode;
  main: ReactNode;
  rightRail: ReactNode;
  /** The independent file sidebar; mounted only while it has open files. */
  filePane: ReactNode;
  composer: ReactNode;
  overlays: ReactNode;
  activity?: ReactNode;
  leftRailOpen: boolean;
  rightRailOpen: boolean;
  filePaneOpen: boolean;
  filePanePosition: FilePanePosition;
  leftRailWidth: number;
  rightRailWidth: number;
  filePaneWidth: number;
}

/**
 * Layout slots. Re-arranging the product (left nav → top nav, right rail →
 * bottom drawer) means editing this file and layout.css only.
 */
export function AppShell({
  topBar,
  leftNav,
  main,
  rightRail,
  filePane,
  composer,
  overlays,
  activity,
  leftRailOpen,
  rightRailOpen,
  filePaneOpen,
  filePanePosition,
  leftRailWidth,
  rightRailWidth,
  filePaneWidth,
}: AppShellProps) {
  const strings = useStrings();
  const classes = [
    "app-shell",
    leftRailOpen ? "" : "left-collapsed",
    rightRailOpen ? "" : "right-collapsed",
    filePanePosition === "inner" ? "file-inner" : "",
  ]
    .filter(Boolean)
    .join(" ");

  // Closed panes get a zero-width column; their content is unmounted or
  // hidden, so the grid stays a single template for every combination.
  const style = {
    "--rail-left-width": `${leftRailOpen ? leftRailWidth : 0}px`,
    "--rail-right-width": `${rightRailOpen ? rightRailWidth : 0}px`,
    "--file-pane-width": `${filePaneOpen ? filePaneWidth : 0}px`,
  } as CSSProperties;

  const resetRail = (kind: RailKind) => {
    if (kind === "file") {
      store.dispatch({
        type: "ui/filePaneWidth",
        width: DEFAULT_FILE_PANE_WIDTH,
        commit: true,
      });
      return;
    }
    store.dispatch({
      type: "ui/railWidth",
      side: kind,
      width: kind === "left" ? DEFAULT_LEFT_RAIL_WIDTH : DEFAULT_RIGHT_RAIL_WIDTH,
      commit: true,
    });
  };

  const onRailDoubleClick = (
    event: ReactMouseEvent<HTMLElement>,
    kind: RailKind,
  ) => {
    // Double-clicking empty rail space resets the width, but never hijack a
    // control inside the rail.
    if ((event.target as HTMLElement).closest("button, a, input, textarea, select")) {
      return;
    }
    resetRail(kind);
  };

  return (
    <div className={classes} style={style}>
      {topBar}
      <aside
        className="left-rail"
        aria-label="Sessions"
        onDoubleClick={(event) => onRailDoubleClick(event, "left")}
      >
        {leftNav}
        {leftRailOpen ? <RailResizer kind="left" /> : null}
      </aside>
      <main className="main-area">{main}</main>
      <aside
        className="right-rail"
        aria-label="Context"
        onDoubleClick={(event) => onRailDoubleClick(event, "right")}
      >
        <div className="right-rail-scroll">{rightRail}</div>
        {rightRailOpen ? <RailResizer kind="right" /> : null}
      </aside>
      {filePaneOpen ? (
        <aside
          className="file-pane"
          aria-label={strings.files.title}
          onDoubleClick={(event) => onRailDoubleClick(event, "file")}
        >
          {filePane}
          <RailResizer kind="file" />
        </aside>
      ) : null}
      {composer ? <div className="composer-dock">{composer}</div> : null}
      {overlays}
      {activity}
    </div>
  );
}
