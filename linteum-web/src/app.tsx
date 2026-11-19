import { useEffect } from "react";
import { store, useAppState } from "./hooks/useStore";
import { useKeyboard } from "./hooks/useKeyboard";
import { ThemeProvider } from "./theme/ThemeProvider";
import { controller } from "./transport/controller";
import { ChatView } from "./ui/chat/ChatView";
import { ActivityDrawer } from "./ui/layout/ActivityDrawer";
import { AppShell } from "./ui/layout/AppShell";
import { ComposerDock } from "./ui/layout/ComposerDock";
import { ConnectScreen } from "./ui/layout/ConnectScreen";
import { FilePane } from "./ui/files/FilePane";
import { LeftNav } from "./ui/layout/LeftNav";
import { OverlayHost } from "./ui/layout/OverlayHost";
import { RightRail } from "./ui/layout/RightRail";
import { SettingsPage } from "./ui/layout/SettingsPage";
import { TopBar } from "./ui/layout/TopBar";
import { WelcomeView } from "./ui/layout/WelcomeView";

let booted = false;

export function App() {
  const state = useAppState();
  useKeyboard();

  // Keep the document language in sync for assistive tech and CSS `:lang()`.
  useEffect(() => {
    document.documentElement.lang = state.ui.locale;
  }, [state.ui.locale]);

  useEffect(() => {
    if (booted) return;
    booted = true;

    if (store.getState().connection.status === "idle") {
      controller.connect();
    }

    // Collapse the rails once on narrow screens; they become drawers.
    if (window.innerWidth < 960 && store.getState().ui.leftRailOpen) {
      store.dispatch({ type: "ui/toggleLeftRail" });
    }
    if (window.innerWidth < 1280 && store.getState().ui.rightRailOpen) {
      store.dispatch({ type: "ui/toggleRightRail" });
    }
  }, []);

  const showConnect = !state.active && state.connection.status !== "ready";
  // A sidebar without tabs would be dead space; the reducer folds both into
  // `filePaneOpen`, this is the render-side guard.
  const filePaneOpen = state.ui.filePaneOpen && state.ui.openFiles.length > 0;

  return (
    <ThemeProvider>
      {showConnect ? (
        <ConnectScreen />
      ) : state.ui.page === "settings" ? (
        <>
          <SettingsPage />
          <OverlayHost />
        </>
      ) : (
        <AppShell
          topBar={<TopBar />}
          leftNav={<LeftNav />}
          main={state.active ? <ChatView /> : <WelcomeView />}
          rightRail={<RightRail />}
          filePane={<FilePane />}
          composer={state.active ? <ComposerDock /> : null}
          overlays={<OverlayHost />}
          activity={state.ui.activityOpen ? <ActivityDrawer /> : null}
          leftRailOpen={state.ui.leftRailOpen}
          rightRailOpen={state.ui.rightRailOpen && state.active !== null}
          filePaneOpen={filePaneOpen}
          filePanePosition={state.ui.filePanePosition}
          leftRailWidth={state.ui.leftRailWidth}
          rightRailWidth={state.ui.rightRailWidth}
          filePaneWidth={state.ui.filePaneWidth}
        />
      )}
    </ThemeProvider>
  );
}
