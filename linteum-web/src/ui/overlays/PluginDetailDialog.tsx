import { Puzzle } from "lucide-react";
import type { ReactNode } from "react";
import { useStrings } from "../../hooks/useStrings";
import { useAppState } from "../../hooks/useStore";
import { controller } from "../../transport/controller";
import { Dialog } from "../common/Dialog";
import { PluginStateBadges } from "../common/PluginBadges";
import { Stat } from "../common/Stat";

function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="plugin-detail-section">
      <h3>{title}</h3>
      {children}
    </section>
  );
}

function BadgeList({
  label,
  items,
  empty,
}: {
  label: string;
  items: readonly string[];
  empty: string;
}) {
  return (
    <div className="plugin-detail-list">
      <span className="plugin-detail-label">{label}</span>
      {items.length === 0 ? (
        <span className="small muted">{empty}</span>
      ) : (
        <div className="row" style={{ flexWrap: "wrap" }}>
          {items.map((item) => (
            <span className="badge" key={item}>
              {item}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export function PluginDetailDialog({
  pluginId,
  onClose,
}: {
  pluginId: string;
  onClose: () => void;
}) {
  const strings = useStrings();
  const { connection, plugins } = useAppState();
  const plugin = plugins.items.find((item) => item.id === pluginId) ?? null;
  const detail = strings.pluginDetail;

  if (!plugin) {
    return (
      <Dialog title={detail.title} onClose={onClose}>
        <p className="small muted">{detail.missing}</p>
      </Dialog>
    );
  }

  const allowed =
    connection.status === "ready" &&
    connection.features.includes("plugins.manage") &&
    plugins.manage;
  const pending = plugins.pendingId === plugin.id;
  const config = Object.entries(plugin.config);
  const lastAction = plugins.lastAction?.id === plugin.id ? plugins.lastAction : null;
  let notice: string | null = null;
  if (plugins.applying) notice = strings.settings.pluginApplying;
  else if (plugins.lastApply) {
    notice = plugins.lastApply.restartRequired
      ? strings.settings.pluginApplyPartial
      : strings.settings.pluginApplied;
  } else if (lastAction) {
    if (!lastAction.changed) notice = strings.settings.pluginUnchanged;
    else if (lastAction.restartRequired) notice = strings.settings.pluginRestartRequired;
    else notice = strings.settings.pluginSaved;
  }

  const footer = (
    <>
      <button className="btn" onClick={onClose}>
        {strings.overlays.close}
      </button>
      {allowed ? (
        <button
          className="btn btn-primary"
          disabled={pending || plugins.applying}
          onClick={() => controller.setPluginEnabled(plugin.id, !plugin.enabled)}
        >
          {pending
            ? strings.settings.pluginPending
            : plugins.applying
              ? strings.settings.pluginApplying
              : plugin.enabled
                ? strings.settings.pluginDisable
                : strings.settings.pluginEnable}
        </button>
      ) : null}
    </>
  );

  return (
    <Dialog title={plugin.name ?? plugin.id} onClose={onClose} wide footer={footer}>
      <div className="plugin-detail-head">
        <Puzzle size={16} />
        <span className="mono">{plugin.id}</span>
        <PluginStateBadges plugin={plugin} showState />
      </div>

      {plugin.description ? (
        <p className="plugin-detail-desc">{plugin.description}</p>
      ) : null}

      {plugins.error ? (
        <div className="form-error">
          {plugins.error.code}: {plugins.error.message}
        </div>
      ) : null}

      {notice ? <p className="small muted">{notice}</p> : null}

      <div className="stat-grid">
        <Stat label={detail.version} value={plugin.version ?? "—"} />
        <Stat label={detail.trust} value={plugin.trust ?? "—"} />
        <Stat label={detail.source} value={plugin.source ?? "—"} />
        <Stat
          label={detail.digest}
          value={plugin.digest ? plugin.digest.slice(0, 16) : "—"}
        />
      </div>

      <DetailSection title={detail.capabilities}>
        <BadgeList label={detail.tools} items={plugin.tools} empty={detail.none} />
        <BadgeList label={detail.skills} items={plugin.skills} empty={detail.none} />
        <BadgeList label={detail.modes} items={plugin.modes} empty={detail.none} />
      </DetailSection>

      <DetailSection title={detail.extensibility}>
        <BadgeList label={detail.hooks} items={plugin.hooks} empty={detail.none} />
        <BadgeList
          label={detail.subscribes}
          items={plugin.subscribes}
          empty={detail.none}
        />
        <BadgeList
          label={detail.publishes}
          items={plugin.publishes}
          empty={detail.none}
        />
      </DetailSection>

      <DetailSection title={detail.configuration}>
        <div className="plugin-detail-list">
          <span className="plugin-detail-label">{detail.configKeys}</span>
          {config.length === 0 ? (
            <span className="small muted">{detail.none}</span>
          ) : (
            <div className="plugin-detail-kv">
              {config.map(([key, source]) => (
                <div className="plugin-detail-kv-row" key={key}>
                  <span className="mono">{key}</span>
                  <span className="small muted">
                    {source === "file"
                      ? detail.sourceFile
                      : source === "default"
                        ? detail.sourceDefault
                        : source}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
        <BadgeList label={detail.secrets} items={plugin.secrets} empty={detail.none} />
      </DetailSection>

      {plugin.warnings.length > 0 || plugin.error ? (
        <DetailSection title={detail.diagnostics}>
          {plugin.warnings.length > 0 ? (
            <div className="plugin-detail-list">
              <span className="plugin-detail-label">{detail.warnings}</span>
              <ul className="plugin-detail-warnings">
                {plugin.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            </div>
          ) : null}
          {plugin.error ? (
            <div className="plugin-detail-error">
              <div className="mono">{plugin.error.code}</div>
              <div>{plugin.error.message}</div>
              {plugin.error.hint ? (
                <div className="small muted">
                  {detail.hint}: {plugin.error.hint}
                </div>
              ) : null}
            </div>
          ) : null}
        </DetailSection>
      ) : null}
    </Dialog>
  );
}
