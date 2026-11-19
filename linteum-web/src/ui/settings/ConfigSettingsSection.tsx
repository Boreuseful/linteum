/**
 * One schema-driven settings section (web-config Phase 3 §6.6).
 *
 * The settings sidebar owns navigation and the global search; this component
 * renders the selected section, loads it on demand, and delegates the form to
 * `SchemaSectionForm`. It never renders its own nav or page-level search.
 */

import { AlertTriangle, RefreshCw } from "lucide-react";
import {
  useEffect,
  useMemo,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";
import { useStrings } from "../../hooks/useStrings";
import { useAppState } from "../../hooks/useStore";
import type { ConfigFieldSpec } from "../../protocol/commands";
import {
  configReadBlocked,
  sectionTitle,
} from "../../state/configuration";
import type { Locale, Strings } from "../../strings";
import { controller } from "../../transport/controller";
import { PanelEmpty, PanelLoading } from "../common/EmptyState";
import { ConfigSyncBanner } from "./ConfigSyncBanner";
import { SchemaSectionForm } from "./SchemaSectionForm";
import { buildConfigSections, fieldMatches } from "./sections";

type SourceFilter = "all" | "default" | "file" | "env" | "cli";

const SOURCE_FILTERS: SourceFilter[] = ["all", "default", "file", "env", "cli"];

function sourceLabel(strings: Strings, value: SourceFilter): string {
  if (value === "all") return strings.settings.configSourceAll;
  const key = `configSource${value[0].toUpperCase()}${value.slice(1)}`;
  return strings.settings[key as keyof Strings["settings"]] as string;
}

function scopeFor(
  configuration: ReturnType<typeof useAppState>["configuration"],
  key: string,
  spec: ConfigFieldSpec,
): string {
  return configuration.runtimeScopes[key] ?? spec.apply_scope;
}

export interface ConfigSettingsSectionProps {
  sectionKey: string;
  /** Global sidebar search; filters the fields of the active section. */
  query?: string;
}

export function ConfigSettingsSection({
  sectionKey,
  query = "",
}: ConfigSettingsSectionProps): ReactElement {
  const strings = useStrings();
  const { connection, configuration, ui } = useAppState();
  const locale = ui.locale as Locale;
  const feature = connection.features.includes("config.settings");
  const connected = connection.status === "ready";
  const [source, setSource] = useState<SourceFilter>("all");

  useEffect(() => {
    setSource("all");
  }, [sectionKey]);

  const section = useMemo(
    () => buildConfigSections(configuration.schema)
      .find((candidate) => candidate.key === sectionKey) ?? null,
    [configuration.schema, sectionKey],
  );

  useEffect(() => {
    if (!section) return;
    const state = configuration.loadedSections[section.key];
    if (!state) controller.loadConfigSection(section.key);
  }, [section?.key, configuration.loadedSections]);

  const loadState = section
    ? configuration.loadedSections[section.key]
    : undefined;
  const readBlocked = configReadBlocked(configuration.error);
  const errors = configuration.issues.filter(
    (issue) => issue.severity === "error",
  );

  const visibleFields = section
    ? section.fields.filter((spec) =>
        fieldMatches(
          spec,
          query,
          source,
          configuration.fields[spec.key],
          locale,
        ),
      )
    : [];
  const hasFilter = query.trim().length > 0 || source !== "all";

  let body: ReactNode = null;
  if (!feature) {
    body = (
      <p className="settings-section-desc">
        {strings.settings.configFeatureMissing}
      </p>
    );
  } else if (!connected) {
    body = <PanelEmpty>{strings.settings.configUnavailable}</PanelEmpty>;
  } else if (!configuration.schema && configuration.loading) {
    body = <PanelLoading label={strings.settings.configLoading} />;
  } else if (!configuration.schema && configuration.error) {
    body = (
      <div className="settings-section">
        <PanelEmpty>
          {configuration.error.code}: {configuration.error.message}
        </PanelEmpty>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => controller.refreshConfig(undefined, true, true)}
        >
          {strings.settings.configReload}
        </button>
      </div>
    );
  } else if (!configuration.schema) {
    body = <PanelLoading label={strings.settings.configLoading} />;
  } else if (!section) {
    body = <PanelEmpty>{strings.settings.configSearchEmpty}</PanelEmpty>;
  } else if (readBlocked) {
    body = (
      <div className="settings-section">
        <PanelEmpty>
          {strings.settings.configLoadFailed} ({configuration.error?.code})
        </PanelEmpty>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => controller.loadConfigSection(section.key)}
        >
          {strings.settings.configReload}
        </button>
      </div>
    );
  } else if (loadState?.error) {
    body = (
      <div className="settings-section">
        <p className="small danger">
          {strings.settings.configSectionLoadFailed}:{" "}
          {loadState.error.code} {loadState.error.message}
        </p>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => controller.loadConfigSection(section.key)}
        >
          <RefreshCw size={13} /> {strings.settings.configReload}
        </button>
      </div>
    );
  } else if (!loadState || loadState.loading) {
    body = <PanelLoading label={strings.settings.configLoading} />;
  } else {
    // Keep the form mounted even when search/source filtering hides every
    // field, so a dirty draft is never discarded by typing.
    body = (
      <>
        {visibleFields.length === 0 && hasFilter ? (
          <PanelEmpty>{strings.settings.configSearchEmpty}</PanelEmpty>
        ) : null}
        <SchemaSectionForm
          key={section.key}
          section={section}
          visibleFields={visibleFields}
          fields={configuration.fields}
          scopeFor={(key: string, spec: ConfigFieldSpec) =>
            scopeFor(configuration, key, spec)
          }
          locale={locale}
          revision={configuration.revision}
          showAllAdvanced={query.trim().length > 0}
        />
      </>
    );
  }

  return (
    <section className="settings-section config-settings">
      {section ? (
        <>
          <div className="config-section-head">
            <h2>{sectionTitle(section, locale)}</h2>
            <span className="small muted mono">{section.key}</span>
            <span className="settings-bar-spacer" />
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => controller.loadConfigSection(section.key)}
            >
              {strings.settings.configReload}
            </button>
          </div>
          <p className="settings-section-desc">
            {strings.settings.configurationHint}
          </p>

          <ConfigSyncBanner sections={[section.key]} />

          {configuration.conflict ? (
            <div className="config-error-banner">
              <p className="small danger">{strings.settings.configConflict}</p>
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => controller.loadConfigSection(section.key)}
              >
                {strings.settings.configReload}
              </button>
            </div>
          ) : null}

          {configuration.manage === false ? (
            <p className="small muted">{strings.settings.configManageDisabled}</p>
          ) : null}
          {configuration.manage && !configuration.writable ? (
            <p className="small muted">{strings.settings.configLocalOnly}</p>
          ) : null}
          {configuration.exists === false ? (
            <p className="small muted">{strings.settings.configCreateFile}</p>
          ) : null}

          {errors.length > 0 ? (
            <div className="config-error-banner">
              <p className="small danger">
                <AlertTriangle size={13} /> {strings.settings.configInvalidTitle}
              </p>
              <ul>
                {errors.slice(0, 6).map((issue) => (
                  <li key={`${issue.key}:${issue.code}`}>
                    <span className="mono">{issue.key || "config"}</span>{" "}
                    <span className="small muted">{issue.message}</span>
                  </li>
                ))}
              </ul>
              <p className="small muted">{strings.settings.configInvalidHint}</p>
            </div>
          ) : null}

          <div className="config-toolbar">
            <div className="config-source-filter">
              {SOURCE_FILTERS.map((value) => (
                <button
                  key={value}
                  type="button"
                  className={`btn btn-sm${
                    source === value ? " btn-primary" : " btn-ghost"
                  }`}
                  onClick={() => setSource(value)}
                >
                  {sourceLabel(strings, value)}
                </button>
              ))}
            </div>
          </div>
        </>
      ) : null}

      {body}
    </section>
  );
}
