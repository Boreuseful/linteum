/**
 * One schema-driven config section (web-config Phase 3 §5.2/§5.5).
 *
 * Owns the section draft, secret tri-state, save/preview/reset and field-level
 * error mapping. The parent supplies the (already filtered) section and fields.
 */

import { useEffect, useMemo, useState, type ReactElement } from "react";
import { useStrings } from "../../hooks/useStrings";
import { store, useAppState } from "../../hooks/useStore";
import type {
  ConfigFieldSpec,
  ConfigFieldValue,
  ConfigSchemaSection,
} from "../../protocol/commands";
import type { ErrorInfo } from "../../protocol/envelope";
import {
  collectConfigChanges,
  configEditValue,
  configErrorKey,
  configRemoteStale,
  fieldTitle,
  type SecretEdit,
  validateConfigChanges,
} from "../../state/configuration";
import type { Locale } from "../../strings";
import { controller } from "../../transport/controller";
import { Dialog } from "../common/Dialog";
import { ConfigFieldControl } from "./ConfigFieldControl";

function isPlainComposite(spec: ConfigFieldSpec): boolean {
  return spec.type === "map" || spec.type === "object" ||
    (spec.type === "list" && spec.item_type === "object");
}

function initialDraft(
  section: ConfigSchemaSection,
  fields: Record<string, ConfigFieldValue>,
): Record<string, unknown> {
  const draft: Record<string, unknown> = {};
  for (const spec of section.fields) {
    if (spec.secret || isPlainComposite(spec)) continue;
    const value = configEditValue(fields[spec.key]);
    draft[spec.key] = Array.isArray(value) ? [...value] : value;
  }
  return draft;
}

function sameDefault(value: unknown, fallback: unknown): boolean {
  try {
    return JSON.stringify(value) === JSON.stringify(fallback);
  } catch {
    return false;
  }
}

function sectionResetKeys(
  section: ConfigSchemaSection,
  fields: Record<string, ConfigFieldValue>,
): string[] {
  const keys: string[] = [];
  for (const spec of section.fields) {
    const field = fields[spec.key];
    if (!field || field.source === "env") continue;
    if (spec.secret) {
      if (field.set) keys.push(spec.key);
      continue;
    }
    if (isPlainComposite(spec)) continue;
    if (!sameDefault(configEditValue(field), spec.default)) keys.push(spec.key);
  }
  return keys;
}

function fieldError(
  error: ErrorInfo | null,
  spec: ConfigFieldSpec,
): string | null {
  const key = configErrorKey(error);
  if (!key) return null;
  if (key === spec.key || key.startsWith(`${spec.key}[`)) {
    return error?.message ?? null;
  }
  return null;
}

export interface SchemaSectionFormProps {
  section: ConfigSchemaSection;
  fields: Record<string, ConfigFieldValue>;
  scopeFor(key: string, spec: ConfigFieldSpec): string;
  locale: Locale;
  revision: string;
  /** Search hits should reveal advanced fields without an extra click. */
  showAllAdvanced?: boolean;
  /** Fields after search/source filtering; draft/reset still use `section`. */
  visibleFields?: ConfigFieldSpec[];
}

export function SchemaSectionForm({
  section,
  fields,
  scopeFor,
  locale,
  revision,
  showAllAdvanced = false,
  visibleFields,
}: SchemaSectionFormProps): ReactElement {
  const strings = useStrings();
  const { configuration } = useAppState();
  const [draft, setDraft] = useState<Record<string, unknown>>(() =>
    initialDraft(section, fields));
  const [secrets, setSecrets] = useState<Record<string, SecretEdit>>({});
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const loadToken = configuration.loadedSections[section.key]?.loadedAtRevision ?? "";

  useEffect(() => {
    setDraft(initialDraft(section, fields));
    setSecrets({});
    store.dispatch({
      type: "intent/configDraft",
      section: section.key,
      dirty: false,
    });
  }, [section.key, loadToken]);

  const changes = useMemo(
    () => collectConfigChanges(draft, fields, secrets),
    [draft, fields, secrets],
  );
  const dirty = Object.keys(changes).length > 0;
  const validation = useMemo(
    () => validateConfigChanges(section.fields, changes),
    [section, changes],
  );
  const hasValidationErrors = Object.keys(validation).length > 0;

  useEffect(() => {
    store.dispatch({
      type: "intent/configDraft",
      section: section.key,
      dirty,
    });
  }, [dirty, section.key]);

  useEffect(() => () => {
    // The draft lives in this component; do not leave a stale global dirty flag
    // behind when the section unmounts.
    store.dispatch({
      type: "intent/configDraft",
      section: section.key,
      dirty: false,
    });
  }, [section.key]);

  const update = (key: string, value: unknown) => {
    setDraft((current) => ({ ...current, [key]: value }));
    store.dispatch({ type: "configuration/previewCleared" });
  };

  const updateSecret = (key: string, edit: SecretEdit) => {
    setSecrets((current) => ({ ...current, [key]: edit }));
    store.dispatch({ type: "configuration/previewCleared" });
  };

  const discard = () => {
    setDraft(initialDraft(section, fields));
    setSecrets({});
    store.dispatch({
      type: "intent/configDraft",
      section: section.key,
      dirty: false,
    });
    store.dispatch({ type: "configuration/previewCleared" });
  };

  const preview = configuration.preview && configuration.preview.changes.some(
    (change) => section.fields.some(
      (spec) => change.key === spec.key || change.key.startsWith(`${spec.key}.`),
    ),
  ) ? configuration.preview : null;
  const previewRevision = preview?.revision ?? revision;
  const canWrite = configuration.writable && !configuration.saving &&
    !configuration.previewing && !configRemoteStale(configuration);
  const resetKeys = sectionResetKeys(section, fields);
  const advancedCount = section.fields.filter((spec) => spec.advanced).length;
  const rendered = visibleFields ?? section.fields;
  const visible = (spec: ConfigFieldSpec) =>
    !spec.advanced || showAdvanced || showAllAdvanced;

  return (
    <section className="settings-section config-section">
      {configuration.error && !configErrorKey(configuration.error) ? (
        <p className="small danger">{configuration.error.message}</p>
      ) : null}

      <div className="settings-form config-form">
        {rendered.filter(visible).map((spec) => {
          const field = fields[spec.key];
          return (
            <ConfigFieldControl
              key={spec.key}
              spec={spec}
              field={field}
              value={spec.secret ? undefined : draft[spec.key]}
              locale={locale}
              strings={strings}
              scope={scopeFor(spec.key, spec)}
              disabled={!canWrite}
              error={
                validation[spec.key]
                  ? strings.settings[validation[spec.key] as keyof typeof strings.settings] as string
                  : fieldError(configuration.error, spec)
              }
              secretEdit={secrets[spec.key] ?? { mode: "unchanged" }}
              onChange={(value) => update(spec.key, value)}
              onSecretEdit={(edit) => updateSecret(spec.key, edit)}
              onReset={
                isPlainComposite(spec)
                  ? undefined
                  : () => controller.unsetConfig([spec.key], revision)
              }
              resetDisabled={
                !canWrite || !field || field.source === "env" ||
                (spec.secret
                  ? !field.set
                  : sameDefault(configEditValue(field), spec.default))
              }
            />
          );
        })}
        {advancedCount > 0 ? (
          <button
            type="button"
            className="btn btn-ghost btn-sm config-advanced"
            onClick={() => setShowAdvanced((current) => !current)}
          >
            {strings.settings.configAdvanced} ({advancedCount})
          </button>
        ) : null}
      </div>

      {preview ? (
        <div className="config-preview">
          {configuration.previewRevisionMismatch ? (
            <p className="small danger">
              {strings.settings.configPreviewStale}
            </p>
          ) : null}

          <p className="small muted">{strings.settings.configPreview}</p>
          <ul className="config-preview-list">
            {preview.changes.map((change) => (
              <li key={change.key}>
                <span className="mono">{change.key}</span>
                <span className="small muted">
                  {" "}
                  {display(change.old)} → {display(change.new)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {configuration.lastSave && !configuration.saving &&
      configuration.lastSave.changes.some((change) =>
        section.fields.some(
          (spec) => change.key === spec.key || change.key.startsWith(`${spec.key}.`),
        ),
      ) ? (
        <p className="small muted">{strings.settings.configSaved}</p>
      ) : null}

      <div className="row config-actions">
        <button
          type="button"
          className="btn btn-sm"
          disabled={!canWrite || resetKeys.length === 0}
          onClick={() => setConfirmReset(true)}
        >
          {strings.settings.configReset}
        </button>
        <span className="settings-bar-spacer" />
        {dirty ? (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={configuration.saving}
            onClick={discard}
          >
            {strings.settings.configDiscard}
          </button>
        ) : null}
        <button
          type="button"
          className="btn btn-sm"
          disabled={!canWrite || !dirty || hasValidationErrors || configuration.previewing}
          onClick={() => controller.previewConfig(section.key, changes)}
        >
          {strings.settings.configPreview}
        </button>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          disabled={!canWrite || !dirty || hasValidationErrors}
          onClick={() => controller.saveConfig(changes, previewRevision)}
        >
          {configuration.saving
            ? strings.settings.configSaving
            : strings.settings.configApply}
        </button>
      </div>

      {confirmReset ? (
        <Dialog
          title={strings.settings.configResetSection}
          onClose={() => setConfirmReset(false)}
          footer={
            <>
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => setConfirmReset(false)}
              >
                {strings.settings.configSecretCancel}
              </button>
              <button
                type="button"
                className="btn btn-danger btn-sm"
                onClick={() => {
                  setConfirmReset(false);
                  controller.unsetConfig(resetKeys, revision);
                }}
              >
                {strings.settings.configReset}
              </button>
            </>
          }
        >
          <p>{strings.settings.configResetConfirm}</p>
        </Dialog>
      ) : null}
    </section>
  );
}

function display(value: unknown): string {
  if (value === null) return "null";
  if (value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
