/**
 * Stale-configuration banner (web-config-phase4 §6.5.4).
 *
 * A `config.changed` push is only a notification. When a local draft exists the
 * client must not refresh it away; it shows this banner instead, disables
 * saving, and offers an explicit reload that discards the draft.
 */

import { AlertTriangle } from "lucide-react";
import { useState, type ReactElement } from "react";
import { useStrings } from "../../hooks/useStrings";
import { useAppState } from "../../hooks/useStore";
import {
  configKeySection,
  configReloadSections,
  configRemoteStale,
} from "../../state/configuration";
import { controller } from "../../transport/controller";
import { Dialog } from "../common/Dialog";

export interface ConfigSyncBannerProps {
  /** Sections a reload must refresh (Providers loads both LLM sections). */
  sections: string[];
  /** Optional handler for "View changes" (jump to the first changed section). */
  onView?: (section: string) => void;
}

export function ConfigSyncBanner({
  sections,
  onView,
}: ConfigSyncBannerProps): ReactElement | null {
  const strings = useStrings();
  const { configuration } = useAppState();
  const [confirming, setConfirming] = useState(false);

  if (!configRemoteStale(configuration)) return null;

  const revision = configuration.remoteRevision ?? "";
  const hint = strings.settings.configRemoteChangedHint.replace(
    "{revision}", revision,
  );
  const firstChanged = configuration.remoteChangedKeys[0]?.key ?? null;
  const targetSection = firstChanged
    ? configKeySection(firstChanged, configuration.schema)
    : null;
  const failed = configuration.error !== null;
  const loading = configuration.loading;

  const reload = () => {
    setConfirming(false);
    // Refresh every loaded section: a scoped read would leave the others
    // silently stale while clearing the global banner.
    controller.refreshConfig(configReloadSections(configuration, sections));
  };

  return (
    <>
      <div className="config-error-banner config-sync-banner">
        <p className="small">
          <AlertTriangle size={13} />{" "}
          {loading
            ? strings.settings.configSync
            : failed
              ? strings.settings.configSyncFailed
              : strings.settings.configRemoteChanged}
        </p>
        <p className="small muted">{hint}</p>
        <div className="row">
          {failed ? (
            <button type="button" className="btn btn-sm" onClick={reload}>
              {strings.settings.configSyncRetry}
            </button>
          ) : (
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => setConfirming(true)}
            >
              {strings.settings.configReloadDiscard}
            </button>
          )}
          {onView && targetSection && !failed ? (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => onView(targetSection)}
            >
              {strings.settings.configViewChanges}
            </button>
          ) : null}
        </div>
      </div>

      {confirming ? (
        <Dialog
          title={strings.settings.configReloadDiscard}
          onClose={() => setConfirming(false)}
          footer={
            <>
              <button
                type="button"
                className="btn"
                onClick={() => setConfirming(false)}
              >
                {strings.overlays.cancel}
              </button>
              <button type="button" className="btn btn-primary" onClick={reload}>
                {strings.settings.configReloadDiscard}
              </button>
            </>
          }
        >
          <p className="small muted">{hint}</p>
        </Dialog>
      ) : null}
    </>
  );
}
