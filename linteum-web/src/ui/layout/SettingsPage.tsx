import {
  ArrowLeft,
  Boxes,
  Cable,
  Cpu,
  Globe,
  Palette,
  Puzzle,
  RefreshCw,
  Search,
  Settings as SettingsIcon,
  SlidersHorizontal,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useStrings } from "../../hooks/useStrings";
import { store, useAppState } from "../../hooks/useStore";
import type { ConfigFieldSpec } from "../../protocol/commands";
import type { ErrorInfo } from "../../protocol/envelope";
import {
  PROFILE_FIELDS,
  collectFallbackChanges,
  collectProfileChanges,
  configEditorDraft,
  configErrorKey,
  configReadBlocked,
  configRemoteStale,
  fallbackValues,
  profileCatalog,
  profileNameError,
  profileValues,
  sectionTitle,
  type SecretEdit,
} from "../../state/configuration";
import { pluginNeedsRestart } from "../../state/plugins";
import type { ThemeChoice } from "../../state/types";
import { LOCALES, type Locale, type Strings } from "../../strings";
import { controller } from "../../transport/controller";
import { Dialog } from "../common/Dialog";
import { PanelEmpty, PanelLoading } from "../common/EmptyState";
import { PluginStateBadges } from "../common/PluginBadges";
import { Stat } from "../common/Stat";
import { ThemeIcon } from "../common/ThemeIcon";
import { ConfigSettingsSection } from "../settings/ConfigSettingsSection";
import { ConfigSyncBanner } from "../settings/ConfigSyncBanner";
import {
  buildConfigSections,
  groupLabel,
  groupOf,
  sectionMatches,
} from "../settings/sections";

type FixedSectionId =
  | "general"
  | "appearance"
  | "connection"
  | "plugins"
  | "providers";

const FIXED_ICONS: Record<FixedSectionId, LucideIcon> = {
  general: Globe,
  appearance: Palette,
  connection: Cable,
  plugins: Puzzle,
  providers: Cpu,
};

const THEMES: ThemeChoice[] = ["dark", "light", "mono", "system"];

/** Dotted prefix of the default (`agent.llm.*`) profile fields. */
const PROFILE_PREFIX = "agent.llm.";

/**
 * Names that collide with reserved keys or lookup paths. Legacy config files
 * may still contain them; the web client shows those cards read-only because
 * `config.profile.set` would target `agent.llm` or a secret leaf instead.
 */
const READ_ONLY_PROFILE_NAMES = new Set([
  "default",
  "api_key",
  "password",
  "secret",
  "token",
]);

interface SecretMeta {
  set: boolean;
  masked: string | null;
}

type ProfileEditor =
  | { kind: "profile"; name: string }
  | { kind: "new-profile" }
  | { kind: "duplicate"; from: string }
  | { kind: "fallback"; index: number }
  | { kind: "new-fallback" };

/** Replace `{name}` placeholders in a catalogue string. */
function fill(template: string, values: Record<string, string | number>): string {
  return Object.entries(values).reduce(
    (text, [key, value]) => text.replace(`{${key}}`, String(value)),
    template,
  );
}

/** Friendly copy for the profile/fallback error codes (§6.3). */
function profileErrorText(strings: Strings, error: ErrorInfo | null): string | null {
  if (!error) return null;
  switch (error.code) {
    case "E_CONFIG_PROFILE_IN_USE": {
      if (error.context?.referenced_by === "sessions") {
        const count =
          typeof error.context?.count === "number" ? error.context.count : 0;
        return fill(strings.settings.profileInUseSessions, { count });
      }
      return strings.settings.profileInUse;
    }
    case "E_CONFIG_PROFILE_EXISTS":
      return strings.settings.profileExists;
    case "E_CONFIG_PROFILE_INCOMPLETE":
      return strings.settings.profileIncomplete;
    case "E_CONFIG_PROFILE_RESERVED":
      return strings.settings.profileReserved;
    case "E_CONFIG_PROVIDER_CONFLICT":
      return strings.settings.profileProviderConflict;
    case "E_CONFIG_PROFILE_NOT_FOUND":
      return strings.settings.profileNotFound;
    default:
      return null;
  }
}

function profileNameErrorText(
  strings: Strings,
  error: "invalid" | "reserved" | "exists",
): string {
  switch (error) {
    case "invalid":
      return strings.settings.profileNameHint;
    case "reserved":
      return strings.settings.profileReserved;
    case "exists":
      return strings.settings.profileExists;
  }
}

function bareProfileField(spec: ConfigFieldSpec): string {
  return spec.key.startsWith(PROFILE_PREFIX)
    ? spec.key.slice(PROFILE_PREFIX.length)
    : spec.key;
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

function themeLabel(strings: Strings, theme: ThemeChoice): string {
  switch (theme) {
    case "dark":
      return strings.settings.dark;
    case "light":
      return strings.settings.light;
    case "mono":
      return strings.settings.mono;
    default:
      return strings.settings.system;
  }
}

function GeneralSection({ locale }: { locale: Locale }) {
  const strings = useStrings();
  return (
    <section className="settings-section">
      <h2>{strings.settings.general}</h2>
      <p className="settings-section-desc">{strings.settings.generalHint}</p>
      <div className="settings-field">
        <span className="label">{strings.settings.language}</span>
        <div className="row">
          {LOCALES.map((item) => (
            <button
              key={item.id}
              className={`btn btn-sm${locale === item.id ? " btn-primary" : ""}`}
              onClick={() => store.dispatch({ type: "ui/locale", locale: item.id })}
            >
              {item.label}
            </button>
          ))}
        </div>
        <span className="small muted">{strings.settings.languageHint}</span>
      </div>
    </section>
  );
}

function AppearanceSection({ theme }: { theme: ThemeChoice }) {
  const strings = useStrings();
  return (
    <section className="settings-section">
      <h2>{strings.settings.appearance}</h2>
      <p className="settings-section-desc">{strings.settings.appearanceHint}</p>
      <div className="row settings-themes">
        {THEMES.map((choice) => (
          <button
            key={choice}
            className={`btn btn-sm${theme === choice ? " btn-primary" : ""}`}
            onClick={() => store.dispatch({ type: "ui/theme", theme: choice })}
          >
            <ThemeIcon theme={choice} size={14} />
            {themeLabel(strings, choice)}
          </button>
        ))}
      </div>
    </section>
  );
}

function ConnectionSection() {
  const strings = useStrings();
  const { connection } = useAppState();
  return (
    <section className="settings-section">
      <h2>{strings.settings.connection}</h2>
      <p className="settings-section-desc">{strings.settings.connectionHint}</p>
      <div className="stat-grid">
        <Stat label={strings.settings.gateway} value={connection.url} />
        <Stat label={strings.settings.status} value={strings.status[connection.status]} />
        <Stat label={strings.settings.instance} value={connection.instanceId ?? "—"} />
        <Stat
          label={strings.settings.protocol}
          value={connection.protocol !== null ? String(connection.protocol) : "—"}
        />
      </div>
      <div className="row">
        <button className="btn btn-sm" onClick={() => controller.connect()}>
          <RefreshCw size={13} /> {strings.commands.reconnect}
        </button>
      </div>
    </section>
  );
}

function PluginsSection() {
  const strings = useStrings();
  const { connection, plugins } = useAppState();
  const connected = connection.status === "ready";
  const manageFeature = connection.features.includes("plugins.manage");
  const runtimeFeature = connection.features.includes("plugins.runtime");
  const allowed = connected && manageFeature && plugins.manage;
  const canApply = allowed && runtimeFeature && plugins.allow !== null;
  const needsRestart = plugins.items.some(pluginNeedsRestart);

  useEffect(() => {
    if (connected) controller.refreshPlugins();
  }, [connected]);

  const lastAction = plugins.lastAction;
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

  return (
    <section className="settings-section">
      <h2>{strings.settings.plugins}</h2>
      <p className="settings-section-desc">{strings.settings.pluginsHint}</p>

      <div className="row">
        <button
          className="btn btn-sm"
          onClick={() => controller.refreshPlugins()}
          disabled={plugins.loading}
        >
          <RefreshCw size={13} /> {strings.settings.pluginsLoad}
        </button>
        {canApply && needsRestart ? (
          <button
            className="btn btn-sm"
            onClick={() => controller.applyPlugins()}
            disabled={plugins.applying || plugins.pendingId !== null}
          >
            <Boxes size={13} /> {strings.settings.pluginsApply}
          </button>
        ) : null}
      </div>

      {!connected ? (
        <p className="settings-section-desc">{strings.settings.pluginsOffline}</p>
      ) : null}

      {connected && !allowed && !plugins.loading && !plugins.error ? (
        <p className="settings-section-desc">
          {manageFeature
            ? strings.settings.pluginsManageDisabled
            : strings.settings.pluginsManageGate}
        </p>
      ) : null}

      {plugins.loading ? <PanelLoading label={strings.sidebar.loading} /> : null}

      {!plugins.loading && plugins.error ? (
        <PanelEmpty>
          {plugins.error.code}: {plugins.error.message}
        </PanelEmpty>
      ) : null}

      {!plugins.loading && !plugins.error && plugins.items.length === 0 ? (
        <PanelEmpty>{strings.settings.pluginsEmpty}</PanelEmpty>
      ) : null}

      {plugins.items.length > 0 ? (
        <div className="settings-plugin-list">
          {plugins.items.map((plugin) => {
            const pending = plugins.pendingId === plugin.id;
            return (
              <div className="settings-plugin" key={plugin.id}>
                <button
                  type="button"
                  className="settings-plugin-main"
                  onClick={() =>
                    store.dispatch({
                      type: "ui/openOverlay",
                      overlay: { kind: "plugin-detail", pluginId: plugin.id },
                    })
                  }
                  aria-label={`${strings.pluginDetail.open}: ${plugin.name ?? plugin.id}`}
                >
                  <span className="settings-plugin-head">
                    <Puzzle size={14} />
                    <span className="settings-plugin-name">{plugin.name ?? plugin.id}</span>
                    <PluginStateBadges plugin={plugin} />
                  </span>
                  <span className="small muted">
                    <span className="mono">{plugin.id}</span>
                    {plugin.version ? <span> · v{plugin.version}</span> : null}
                  </span>
                  {plugin.description ? (
                    <span className="small muted">{plugin.description}</span>
                  ) : null}
                </button>
                {allowed ? (
                  <button
                    className="btn btn-sm"
                    disabled={pending || plugins.applying}
                    onClick={() => controller.setPluginEnabled(plugin.id, !plugin.enabled)}
                  >
                    {pending
                      ? strings.settings.pluginPending
                      : plugin.enabled
                        ? strings.settings.pluginDisable
                        : strings.settings.pluginEnable}
                  </button>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}

      {notice ? <p className="settings-section-desc">{notice}</p> : null}
    </section>
  );
}

function sourceLabel(strings: Strings, source: string): string {
  switch (source) {
    case "file":
      return strings.settings.configSourceFile;
    case "env":
      return strings.settings.configSourceEnv;
    case "cli":
      return strings.settings.configSourceCli;
    default:
      return strings.settings.configSourceDefault;
  }
}

function scopeLabel(strings: Strings, scope: string): string {
  switch (scope) {
    case "live":
      return strings.settings.configScopeLive;
    case "next_connection":
      return strings.settings.configScopeNextConnection;
    case "restart":
      return strings.settings.configScopeRestart;
    case "client":
      return strings.settings.configScopeClient;
    case "noop":
      return strings.settings.configScopeNoop;
    default:
      return strings.settings.configScopeNewSession;
  }
}

function providerFieldLabel(strings: Strings, spec: ConfigFieldSpec): string {
  switch (spec.key) {
    case "agent.llm.provider":
      return strings.settings.provider;
    case "agent.llm.model":
      return strings.settings.providerModel;
    case "agent.llm.base_url":
      return strings.settings.providerBaseUrl;
    case "agent.llm.api_key":
      return strings.settings.providerApiKey;
    case "agent.llm.openai_mode":
      return strings.settings.providerOpenaiMode;
    case "agent.llm.timeout":
      return strings.settings.configTimeout;
    case "agent.llm.temperature":
      return strings.settings.configTemperature;
    case "agent.llm.max_tokens":
      return strings.settings.configMaxTokens;
    case "agent.llm.top_p":
      return strings.settings.configTopP;
    case "agent.llm.reasoning_effort":
      return strings.settings.configReasoningEffort;
    case "agent.llm.thinking":
      return strings.settings.configThinking;
    default:
      return spec.title;
  }
}

function ProfileFields({
  specs,
  values,
  draft,
  secrets,
  secretMeta,
  disabled,
  locked,
  fieldError,
  onChange,
  onSecret,
}: {
  specs: ConfigFieldSpec[];
  values: Record<string, unknown>;
  draft: Record<string, unknown>;
  secrets: Record<string, SecretEdit>;
  secretMeta: Record<string, SecretMeta>;
  disabled: boolean;
  locked: ReadonlySet<string>;
  fieldError: { field: string; message: string } | null;
  onChange: (field: string, value: unknown) => void;
  onSecret: (field: string, edit: SecretEdit) => void;
}) {
  const strings = useStrings();

  return (
    <>
      {specs.map((spec) => {
        const field = bareProfileField(spec);
        const fieldLocked = locked.has(field);
        const fieldDisabled = disabled || fieldLocked;
        const value = Object.prototype.hasOwnProperty.call(draft, field)
          ? draft[field]
          : values[field] !== undefined
            ? values[field]
            : spec.default;
        const id = `profile-field-${field}`;
        const meta = secretMeta[field];

        let control: ReactNode;
        if (spec.secret) {
          const edit = secrets[field] ?? { mode: "unchanged" as const };
          if (edit.mode === "replace") {
            control = (
              <div className="row">
                <input
                  id={id}
                  className="input"
                  type="password"
                  autoComplete="off"
                  value={edit.value}
                  disabled={fieldDisabled}
                  onChange={(event) =>
                    onSecret(field, {
                      mode: "replace",
                      value: event.target.value,
                    })
                  }
                />
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => onSecret(field, { mode: "unchanged" })}
                >
                  {strings.settings.configSecretCancel}
                </button>
              </div>
            );
          } else {
            control = (
              <div className="row">
                <input
                  id={id}
                  className="input"
                  type="password"
                  value=""
                  readOnly
                  disabled={fieldDisabled}
                  placeholder={
                    meta?.set
                      ? meta.masked ?? strings.settings.configSecretSet
                      : strings.settings.providerNone
                  }
                />
                {!disabled && !fieldLocked ? (
                  <>
                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={() =>
                        onSecret(field, { mode: "replace", value: "" })
                      }
                    >
                      {strings.settings.configSecretReplace}
                    </button>
                    {spec.nullable && meta?.set ? (
                      <button
                        type="button"
                        className="btn btn-sm"
                        onClick={() => onSecret(field, { mode: "clear" })}
                      >
                        {strings.settings.configSecretClear}
                      </button>
                    ) : null}
                  </>
                ) : null}
                {edit.mode === "clear" ? (
                  <span className="small muted">
                    {strings.settings.configSecretWillClear}
                  </span>
                ) : null}
              </div>
            );
          }
        } else if (spec.type === "enum" && spec.allow_custom) {
          control = (
            <>
              <input
                id={id}
                className="input"
                list={`${id}-options`}
                value={value === null || value === undefined ? "" : String(value)}
                disabled={fieldDisabled}
                onChange={(event) => onChange(field, event.target.value)}
              />
              <datalist id={`${id}-options`}>
                {spec.enum.map((option) => (
                  <option key={option} value={option} />
                ))}
              </datalist>
            </>
          );
        } else if (spec.type === "enum") {
          control = (
            <select
              id={id}
              className="input"
              value={value === null || value === undefined ? "" : String(value)}
              disabled={fieldDisabled}
              onChange={(event) => {
                const next = event.target.value;
                onChange(field, spec.nullable && next === "" ? null : next);
              }}
            >
              {spec.nullable ? (
                <option value="">{strings.settings.providerNone}</option>
              ) : null}
              {spec.enum.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          );
        } else if (spec.type === "bool") {
          if (spec.nullable) {
            control = (
              <select
                id={id}
                className="input"
                value={
                  value === null || value === undefined
                    ? ""
                    : value
                      ? "true"
                      : "false"
                }
                disabled={fieldDisabled}
                onChange={(event) => {
                  const next = event.target.value;
                  onChange(field, next === "" ? null : next === "true");
                }}
              >
                <option value="">{strings.settings.providerNone}</option>
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            );
          } else {
            control = (
              <input
                id={id}
                type="checkbox"
                checked={value === true}
                disabled={fieldDisabled}
                onChange={(event) => onChange(field, event.target.checked)}
              />
            );
          }
        } else if (spec.type === "int" || spec.type === "float") {
          control = (
            <input
              id={id}
              className="input"
              type="number"
              value={value === null || value === undefined ? "" : String(value)}
              min={spec.minimum ?? undefined}
              max={spec.maximum ?? undefined}
              step={spec.type === "float" ? "any" : 1}
              disabled={fieldDisabled}
              onChange={(event) => {
                const text = event.target.value;
                onChange(
                  field,
                  text === "" ? (spec.nullable ? null : "") : Number(text),
                );
              }}
            />
          );
        } else if (
          spec.type === "list" ||
          spec.type === "map" ||
          spec.type === "object"
        ) {
          control = (
            <span className="settings-readonly">
              {strings.settings.configCompositeReadonly}
            </span>
          );
        } else {
          control = (
            <input
              id={id}
              className="input"
              type="text"
              value={value === null || value === undefined ? "" : String(value)}
              disabled={fieldDisabled}
              onChange={(event) => {
                const text = event.target.value;
                onChange(field, spec.nullable && text === "" ? null : text);
              }}
            />
          );
        }

        return (
          <div className="settings-field" key={spec.key}>
            <label className="label" htmlFor={id}>
              {providerFieldLabel(strings, spec)}
            </label>
            {control}
            {fieldLocked ? (
              <span className="small muted">
                {strings.settings.configEnvLocked}
              </span>
            ) : null}
            {spec.description ? (
              <span className="small muted">{spec.description}</span>
            ) : null}
            {fieldError?.field === field ? (
              <span className="small muted">{fieldError.message}</span>
            ) : null}
          </div>
        );
      })}
    </>
  );
}

function ProvidersSection() {
  const strings = useStrings();
  const { connection, configuration } = useAppState();
  const connected = connection.status === "ready";
  const supported = connection.features.includes("config");
  // Profile/fallback commands are only safe when the gateway advertises the
  // dedicated feature; otherwise fall back to the plain agent.llm editor.
  const profilesSupported =
    supported && connection.features.includes("config.profiles");
  const [editor, setEditor] = useState<ProfileEditor | null>(null);
  const [nameDraft, setNameDraft] = useState("");
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [secrets, setSecrets] = useState<Record<string, SecretEdit>>({});
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);

  // Providers must report its draft like the schema sections do, or a
  // `config.changed` push would auto-refresh (and reset) the editor (§7.4).
  const editorDraft = useMemo(
    () => configEditorDraft(editor, configuration.fields, draft, secrets),
    [editor, configuration.fields, draft, secrets],
  );
  useEffect(() => {
    if (!editorDraft) return;
    store.dispatch({
      type: "intent/configDraft",
      section: editorDraft.section,
      dirty: editorDraft.dirty,
    });
  }, [editorDraft?.section, editorDraft?.dirty]);
  useEffect(() => () => {
    if (!editorDraft) return;
    store.dispatch({
      type: "intent/configDraft",
      section: editorDraft.section,
      dirty: false,
    });
  }, [editorDraft?.section]);

  useEffect(() => {
    if (connected && supported) {
      controller.refreshConfig(["agent.llm", "agent.profiles"]);
    }
  }, [connected, supported, connection.instanceId]);

  // A successful save refreshes `config.get`; drop the local draft and close
  // the editor so the cards below show the stored values.
  useEffect(() => {
    if (configuration.lastSave) {
      setEditor(null);
      setNameDraft("");
      setDraft({});
      setSecrets({});
      setPendingDelete(null);
    }
  }, [configuration.lastSave]);

  useEffect(() => {
    setEditor(null);
    setNameDraft("");
    setDraft({});
    setSecrets({});
    setPendingDelete(null);
  }, [connection.instanceId]);

  // Hard references (default_profile / subagent config) cannot be confirmed;
  // drop the pending delete when the gateway reports one of those.
  useEffect(() => {
    if (!pendingDelete || !configuration.error) return;
    if (
      configuration.error.code === "E_CONFIG_PROFILE_IN_USE" &&
      configuration.error.context?.referenced_by === "sessions"
    ) {
      return;
    }
    setPendingDelete(null);
  }, [configuration.error, pendingDelete]);

  const section = configuration.schema?.sections.find(
    (candidate) => candidate.key === "agent.llm",
  );
  const profileSpecs = section
    ? PROFILE_FIELDS.map((field) =>
        section.fields.find((spec) => spec.key === `${PROFILE_PREFIX}${field}`),
      ).filter((spec): spec is ConfigFieldSpec => spec !== undefined)
    : [];

  const { defaultProfile, names: catalogNames } = profileCatalog(
    configuration.fields,
  );
  // `agent.profiles.default` is reserved and would collide with the
  // `agent.llm` default card / select option.
  const names = catalogNames.filter((name) => name !== "default");
  const defaultField = configuration.fields["agent.default_profile"];
  const profilesField = configuration.fields["agent.profiles"];
  const fallbacksField = configuration.fields["agent.fallbacks"];
  const profilesLocked = profilesField?.source === "env";
  const fallbacksLocked = fallbacksField?.source === "env";
  const defaultLocked = defaultField?.source === "env";
  const fallbackList = Array.isArray(fallbacksField?.value)
    ? fallbacksField.value
    : [];
  const defaultValues = profileValues(configuration.fields, "default");

  const canMutate =
    connected &&
    supported &&
    configuration.writable &&
    !configuration.saving &&
    !configuration.previewing &&
    !configRemoteStale(configuration) &&
    !configReadBlocked(configuration.error);

  const profileIssues = configuration.issues.filter(
    (issue) =>
      issue.key === "agent.llm" ||
      issue.key === "agent.default_profile" ||
      issue.key.startsWith("agent.profiles") ||
      issue.key.startsWith("agent.fallbacks") ||
      issue.key.startsWith(PROFILE_PREFIX),
  );

  const errorKey = configErrorKey(configuration.error);

  const clearEditor = () => {
    setEditor(null);
    setNameDraft("");
    setDraft({});
    setSecrets({});
  };

  const openProfile = (name: string) => {
    setEditor({ kind: "profile", name });
    setNameDraft("");
    setDraft({});
    setSecrets({});
  };

  const openNewProfile = () => {
    setEditor({ kind: "new-profile" });
    setNameDraft("");
    setDraft({ provider: "openai", model: "" });
    setSecrets({});
  };

  const openDuplicate = (from: string) => {
    setEditor({ kind: "duplicate", from });
    setNameDraft("");
    setDraft({});
    setSecrets({});
  };

  const openFallback = (index: number) => {
    setEditor({ kind: "fallback", index });
    setNameDraft("");
    setDraft({});
    setSecrets({});
  };

  const openNewFallback = () => {
    setEditor({ kind: "new-fallback" });
    setNameDraft("");
    setDraft({ provider: "openai", model: "" });
    setSecrets({});
  };

  const updateDraft = (field: string, value: unknown) => {
    setDraft((previous) => ({ ...previous, [field]: value }));
  };

  const updateSecret = (field: string, edit: SecretEdit) => {
    setSecrets((previous) => ({ ...previous, [field]: edit }));
  };

  const editorExisting = (): Record<string, unknown> => {
    if (!editor) return {};
    if (editor.kind === "profile") {
      return profileValues(configuration.fields, editor.name);
    }
    if (editor.kind === "fallback") {
      return fallbackValues(configuration.fields, editor.index);
    }
    return {};
  };

  const requestDelete = (name: string) => {
    setPendingDelete(name);
    controller.deleteProfile(name);
  };

  const cancelDelete = () => {
    setPendingDelete(null);
    controller.refreshConfig(["agent.llm", "agent.profiles"]);
  };

  const pendingDeleteError =
    pendingDelete !== null &&
    configuration.error?.code === "E_CONFIG_PROFILE_IN_USE" &&
    configuration.error.context?.referenced_by === "sessions"
      ? configuration.error
      : null;
  const pendingDeleteCountValue = pendingDeleteError?.context?.count;
  const pendingDeleteCount =
    typeof pendingDeleteCountValue === "number" ? pendingDeleteCountValue : 0;

  let notice: string | null = null;
  if (configuration.saving) {
    notice = strings.settings.configSaving;
  } else if (configuration.lastSave) {
    const last = configuration.lastSave;
    const scopes = Object.values(last.apply.runtime);
    if (!last.changed) notice = strings.settings.configUnchanged;
    else if (scopes.includes("restart")) {
      notice = strings.settings.configRestartRequired;
    } else if (scopes.every((scope) => scope === "new_session")) {
      notice = strings.settings.configSavedNewSession;
    } else {
      notice = strings.settings.configSaved;
    }
  }

  const renderEditor = () => {
    if (!editor) return null;
    const current = editor;
    if (
      current.kind === "profile" &&
      current.name !== "default" &&
      profilesLocked
    ) {
      return null;
    }
    if (
      (current.kind === "fallback" || current.kind === "new-fallback") &&
      fallbacksLocked
    ) {
      return null;
    }

    const cancelButton = (
      <button className="btn" onClick={clearEditor}>
        {strings.settings.configSecretCancel}
      </button>
    );

    if (current.kind === "duplicate") {
      const nameError =
        nameDraft === "" ? null : profileNameError(nameDraft, names);
      return (
        <Dialog
          title={`${strings.settings.profileDuplicate}: ${current.from}`}
          onClose={clearEditor}
          footer={
            <>
              <button
                className="btn btn-primary"
                disabled={!canMutate || nameDraft === "" || nameError !== null}
                onClick={() => controller.duplicateProfile(current.from, nameDraft)}
              >
                {strings.settings.profileDuplicate}
              </button>
              {cancelButton}
            </>
          }
        >
          <div className="settings-editor">
            <div className="settings-field">
              <label className="label" htmlFor="profile-new-name">
                {strings.settings.profileName}
              </label>
              <input
                id="profile-new-name"
                className="input"
                value={nameDraft}
                spellCheck={false}
                autoFocus
                onChange={(event) => setNameDraft(event.target.value)}
              />
              <span className="small muted">
                {strings.settings.profileNameHint}
              </span>
              {nameError ? (
                <span className="small muted">
                  {profileNameErrorText(strings, nameError)}
                </span>
              ) : null}
            </div>
          </div>
        </Dialog>
      );
    }

    const existing = editorExisting();
    const isFallback =
      current.kind === "fallback" || current.kind === "new-fallback";
    const changes = isFallback
      ? collectFallbackChanges(draft, existing, secrets)
      : collectProfileChanges(draft, existing, secrets);
    const dirty = Object.keys(changes).length > 0;
    const values: Record<string, unknown> = { ...existing, ...draft };
    const provider =
      typeof values.provider === "string" ? values.provider.trim() : "";
    const model = typeof values.model === "string" ? values.model.trim() : "";
    const complete = provider !== "" && model !== "";
    const isNewProfile = current.kind === "new-profile";
    const nameError =
      isNewProfile && nameDraft !== ""
        ? profileNameError(nameDraft, names)
        : null;
    const canSave =
      canMutate &&
      dirty &&
      complete &&
      (!isNewProfile || (nameDraft !== "" && nameError === null));

    // Secrets in `agent.profiles` / `agent.fallbacks` are masked maps; only
    // `agent.llm.*` exposes per-leaf `set` / `masked` metadata.
    const secretMeta: Record<string, SecretMeta> = {};
    for (const spec of profileSpecs) {
      if (!spec.secret) continue;
      const field = bareProfileField(spec);
      if (current.kind === "profile" && current.name === "default") {
        const leaf = configuration.fields[spec.key];
        secretMeta[field] = {
          set: leaf?.set === true,
          masked: leaf?.masked ?? null,
        };
      } else {
        const raw = existing[field];
        secretMeta[field] = {
          set: raw !== null && raw !== undefined && raw !== "",
          masked: typeof raw === "string" ? raw : null,
        };
      }
    }

    const locked = new Set<string>();
    if (current.kind === "profile" && current.name === "default") {
      for (const spec of profileSpecs) {
        if (configuration.fields[spec.key]?.source === "env") {
          locked.add(bareProfileField(spec));
        }
      }
    }

    let fieldError: { field: string; message: string } | null = null;
    const errorPrefix =
      current.kind === "profile"
        ? current.name === "default"
          ? "agent.llm"
          : `agent.profiles.${current.name}`
        : current.kind === "new-profile"
          ? `agent.profiles.${nameDraft}`
          : current.kind === "fallback"
            ? `agent.fallbacks.${current.index}`
            : null;
    if (
      configuration.error &&
      errorKey &&
      errorPrefix &&
      errorKey.startsWith(`${errorPrefix}.`)
    ) {
      const field = errorKey.slice(errorPrefix.length + 1);
      if ((PROFILE_FIELDS as readonly string[]).includes(field)) {
        fieldError = { field, message: configuration.error.message };
      }
    }

    const save = () => {
      if (current.kind === "profile") {
        if (current.name === "default" && !profilesSupported) {
          // Without config.profiles the gateway has no profile commands;
          // edit agent.llm through the generic dotted config.set.
          const dotted: Record<string, unknown> = {};
          for (const [field, value] of Object.entries(changes)) {
            dotted[`${PROFILE_PREFIX}${field}`] = value;
          }
          controller.saveConfig(dotted, configuration.revision);
          return;
        }
        controller.saveProfile(current.name, changes);
      } else if (current.kind === "new-profile") {
        controller.saveProfile(nameDraft, changes);
      } else if (current.kind === "fallback") {
        controller.saveFallback(changes, current.index);
      } else if (current.kind === "new-fallback") {
        controller.saveFallback(changes);
      }
    };

    const title =
      current.kind === "profile"
        ? `${strings.settings.profileEdit}: ${current.name}`
        : current.kind === "new-profile"
          ? strings.settings.profileAdd
          : current.kind === "fallback"
            ? `${strings.settings.fallbackEdit}: ${strings.settings.fallbackLabel} ${current.index + 1}`
            : strings.settings.fallbackAdd;

    return (
      <Dialog
        title={title}
        onClose={clearEditor}
        wide
        footer={
          <>
            <button
              className="btn btn-primary"
              onClick={save}
              disabled={!canSave}
            >
              {configuration.saving
                ? strings.settings.configSaving
                : strings.settings.configSave}
            </button>
            {cancelButton}
          </>
        }
      >
        <div className="settings-editor">
        {isNewProfile ? (
          <div className="settings-field">
            <label className="label" htmlFor="profile-new-name">
              {strings.settings.profileName}
            </label>
            <input
              id="profile-new-name"
              className="input"
              value={nameDraft}
              spellCheck={false}
              autoFocus
              onChange={(event) => setNameDraft(event.target.value)}
            />
            <span className="small muted">
              {strings.settings.profileNameHint}
            </span>
            {nameError ? (
              <span className="small muted">
                {profileNameErrorText(strings, nameError)}
              </span>
            ) : null}
          </div>
        ) : null}
        <ProfileFields
          specs={profileSpecs}
          values={values}
          draft={draft}
          secrets={secrets}
          secretMeta={secretMeta}
          disabled={!canMutate}
          locked={locked}
          fieldError={fieldError}
          onChange={updateDraft}
          onSecret={updateSecret}
        />
        {!complete ? (
          <span className="small muted">
            {strings.settings.profileIncomplete}
          </span>
        ) : null}
        {configuration.error ? (
          <span className="small muted">
            {profileErrorText(strings, configuration.error) ??
              `${configuration.error.code}: ${configuration.error.message}`}
            {errorKey ? ` · ${errorKey}` : ""}
          </span>
        ) : null}
        </div>
      </Dialog>
    );
  };

  return (
    <section className="settings-section">
      <h2>{strings.settings.providers}</h2>
      <p className="settings-section-desc">{strings.settings.providersHint}</p>

      <ConfigSyncBanner sections={["agent.llm", "agent.profiles"]} />

      {!connected ? (
        <p className="settings-section-desc">{strings.settings.pluginsOffline}</p>
      ) : null}

      {connected && !supported ? (
        <p className="settings-section-desc">{strings.settings.providersPending}</p>
      ) : null}

      {connected && supported && !profilesSupported ? (
        <p className="settings-section-desc">
          {strings.settings.profilesUnsupported}
        </p>
      ) : null}

      {connected && supported && configuration.schema && !configuration.manage ? (
        <p className="settings-section-desc">
          {strings.settings.configManageDisabled}
        </p>
      ) : null}

      {connected &&
      supported &&
      configuration.schema &&
      configuration.manage &&
      !configuration.writable ? (
        <p className="settings-section-desc">
          {strings.settings.configLocalOnly}
        </p>
      ) : null}

      {connected && supported && configuration.schema && !configuration.exists ? (
        <p className="settings-section-desc">
          {strings.settings.configCreateFile}
        </p>
      ) : null}

      {configuration.loading && !configuration.schema ? (
        <PanelLoading label={strings.sidebar.loading} />
      ) : null}

      {configuration.error ? (
        <p className="settings-section-desc">
          {profileErrorText(strings, configuration.error) ??
            (configReadBlocked(configuration.error)
              ? strings.settings.configLoadFailed
              : configuration.schema
                ? strings.settings.configSaveFailed
                : strings.settings.configLoadFailed)}{" "}
          ({configuration.error.code}: {configuration.error.message})
          {errorKey ? ` · ${errorKey}` : ""}
          {configuration.path ? ` — ${configuration.path}` : ""}
        </p>
      ) : null}

      {configuration.conflict ? (
        <div className="row">
          <span className="settings-section-desc">
            {strings.settings.configConflict}
          </span>
          <button
            className="btn btn-sm"
            onClick={() => controller.refreshConfig(["agent.llm", "agent.profiles"])}
          >
            <RefreshCw size={13} /> {strings.settings.configReload}
          </button>
        </div>
      ) : null}

      {section ? (
        <>
          {profilesSupported ? (
            <div className="settings-field">
              <label className="label" htmlFor="default-profile">
                {strings.settings.profileCurrentDefault}
              </label>
              <select
                id="default-profile"
                className="input"
                value={defaultProfile}
                disabled={!canMutate || defaultLocked}
                onChange={(event) =>
                  controller.saveConfig(
                    { "agent.default_profile": event.target.value },
                    configuration.revision,
                  )
                }
              >
                <option value="default">default</option>
                {defaultProfile !== "default" && !names.includes(defaultProfile) ? (
                  <option value={defaultProfile}>{defaultProfile}</option>
                ) : null}
                {names.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
              <span className="small muted">
                {sourceLabel(strings, defaultField?.source ?? "default")} ·{" "}
                {scopeLabel(
                  strings,
                  configuration.runtimeScopes["agent.default_profile"] ??
                    "new_session",
                )}
                {defaultLocked ? ` · ${strings.settings.configEnvLocked}` : ""}
              </span>
            </div>
          ) : null}

          {profilesSupported ? (
            <>
              <h3>{strings.settings.profilesTitle}</h3>
              <span className="small muted">
                {sourceLabel(strings, profilesField?.source ?? "default")} ·{" "}
                {scopeLabel(
                  strings,
                  configuration.runtimeScopes["agent.profiles"] ?? "new_session",
                )}
                {profilesLocked ? ` · ${strings.settings.configEnvLocked}` : ""}
              </span>
            </>
          ) : null}

          <div className="settings-profile-list">
            <div className="settings-profile">
              <div className="settings-profile-info">
                <div className="settings-profile-head">
                  <span className="badge">default</span>
                  {defaultProfile === "default" ? (
                    <span className="badge badge-accent">
                      {strings.settings.profileDefault}
                    </span>
                  ) : null}
                  <span className="mono small muted">agent.llm</span>
                </div>
                <span className="small muted">
                  {displayValue(defaultValues.provider)} /{" "}
                  {displayValue(defaultValues.model)}
                </span>
              </div>
              {!profilesLocked ? (
                <div className="settings-profile-actions">
                  <button
                    className="btn btn-sm"
                    disabled={!canMutate}
                    onClick={() => openProfile("default")}
                  >
                    {strings.settings.profileEdit}
                  </button>
                  {profilesSupported ? (
                    <button
                      className="btn btn-sm"
                      disabled={!canMutate}
                      onClick={() => openDuplicate("default")}
                    >
                      {strings.settings.profileDuplicate}
                    </button>
                  ) : null}
                </div>
              ) : null}
            </div>

            {profilesSupported
              ? names.map((name) => {
                  const values = profileValues(configuration.fields, name);
                  const readOnly = READ_ONLY_PROFILE_NAMES.has(name);
                  return (
                    <div className="settings-profile" key={name}>
                      <div className="settings-profile-info">
                        <div className="settings-profile-head">
                          <span className="badge">{name}</span>
                          {name === defaultProfile ? (
                            <span className="badge badge-accent">
                              {strings.settings.profileDefault}
                            </span>
                          ) : null}
                        </div>
                        <span className="small muted">
                          {displayValue(values.provider)} /{" "}
                          {displayValue(values.model)}
                        </span>
                        {readOnly ? (
                          <span className="small muted">
                            {strings.settings.profileReadOnly}
                          </span>
                        ) : null}
                      </div>
                      {!profilesLocked && !readOnly ? (
                        <div className="settings-profile-actions">
                          <button
                            className="btn btn-sm"
                            disabled={!canMutate}
                            onClick={() => openProfile(name)}
                          >
                            {strings.settings.profileEdit}
                          </button>
                          <button
                            className="btn btn-sm"
                            disabled={!canMutate}
                            onClick={() => openDuplicate(name)}
                          >
                            {strings.settings.profileDuplicate}
                          </button>
                          <button
                            className="btn btn-sm"
                            disabled={
                              !canMutate ||
                              name === defaultProfile ||
                              defaultLocked
                            }
                            onClick={() =>
                              controller.saveConfig(
                                { "agent.default_profile": name },
                                configuration.revision,
                              )
                            }
                          >
                            {strings.settings.profileSetDefault}
                          </button>
                          <button
                            className="btn btn-sm btn-danger"
                            disabled={!canMutate}
                            onClick={() => requestDelete(name)}
                          >
                            {strings.settings.profileDelete}
                          </button>
                        </div>
                      ) : null}
                    </div>
                  );
                })
              : null}
          </div>

          {profilesSupported && !profilesLocked ? (
            <div className="row">
              <button
                className="btn btn-sm"
                disabled={!canMutate}
                onClick={openNewProfile}
              >
                {strings.settings.profileAdd}
              </button>
            </div>
          ) : null}

          {profilesSupported ? (
            <>
              <h3>{strings.settings.fallbacksTitle}</h3>
              <span className="small muted">
                {sourceLabel(strings, fallbacksField?.source ?? "default")} ·{" "}
                {scopeLabel(
                  strings,
                  configuration.runtimeScopes["agent.fallbacks"] ?? "new_session",
                )}
                {fallbacksLocked ? ` · ${strings.settings.configEnvLocked}` : ""}
              </span>

              <div className="settings-profile-list">
                {fallbackList.map((_, index) => {
                  const values = fallbackValues(configuration.fields, index);
                  return (
                    <div className="settings-profile" key={index}>
                      <div className="settings-profile-info">
                        <div className="settings-profile-head">
                          <span className="settings-profile-name">
                            {strings.settings.fallbackLabel} {index + 1}
                          </span>
                        </div>
                        <span className="small muted">
                          {displayValue(values.provider)} /{" "}
                          {displayValue(values.model)}
                        </span>
                      </div>
                      {!fallbacksLocked ? (
                        <div className="settings-profile-actions">
                          <button
                            className="btn btn-sm"
                            disabled={!canMutate}
                            onClick={() => openFallback(index)}
                          >
                            {strings.settings.fallbackEdit}
                          </button>
                          <button
                            className="btn btn-sm"
                            disabled={!canMutate || index === 0}
                            onClick={() => controller.moveFallback(index, index - 1)}
                          >
                            {strings.settings.fallbackMoveUp}
                          </button>
                          <button
                            className="btn btn-sm"
                            disabled={
                              !canMutate || index === fallbackList.length - 1
                            }
                            onClick={() => controller.moveFallback(index, index + 1)}
                          >
                            {strings.settings.fallbackMoveDown}
                          </button>
                          <button
                            className="btn btn-sm btn-danger"
                            disabled={!canMutate}
                            onClick={() => controller.deleteFallback(index)}
                          >
                            {strings.settings.fallbackDelete}
                          </button>
                        </div>
                      ) : null}
                    </div>
                  );
                })}
              </div>

              {!fallbacksLocked ? (
                <div className="row">
                  <button
                    className="btn btn-sm"
                    disabled={!canMutate}
                    onClick={openNewFallback}
                  >
                    {strings.settings.fallbackAdd}
                  </button>
                </div>
              ) : null}
            </>
          ) : null}

          {profileIssues.length > 0 ? (
            <div className="settings-issues">
              {profileIssues.map((issue, index) => {
                const message =
                  profileErrorText(strings, {
                    code: issue.code,
                    message: issue.message,
                  }) ?? issue.message;
                return (
                  <p key={`${issue.key}-${index}`} className="small muted">
                    {issue.severity}: {message} ({issue.key})
                  </p>
                );
              })}
            </div>
          ) : null}

          {pendingDelete && pendingDeleteError ? (
            <div className="settings-confirm">
              <p className="settings-section-desc">
                {fill(strings.settings.profileInUseSessions, {
                  count: pendingDeleteCount,
                })}{" "}
                {fill(strings.settings.profileDeleteConfirmBody, {
                  name: pendingDelete,
                })}
              </p>
              <div className="row">
                <button
                  className="btn btn-sm btn-danger"
                  disabled={!canMutate}
                  onClick={() => {
                    if (pendingDelete) {
                      controller.deleteProfile(pendingDelete, true);
                    }
                  }}
                >
                  {strings.settings.profileDeleteConfirm}
                </button>
                <button className="btn btn-sm" onClick={cancelDelete}>
                  {strings.settings.configSecretCancel}
                </button>
              </div>
            </div>
          ) : null}

          {renderEditor()}

          {notice ? <p className="settings-section-desc">{notice}</p> : null}
        </>
      ) : null}

      {connected &&
      supported &&
      !configuration.loading &&
      !section &&
      !configuration.error ? (
        <PanelEmpty>{strings.settings.configUnavailable}</PanelEmpty>
      ) : null}
    </section>
  );
}

export function SettingsPage() {
  const strings = useStrings();
  const { connection, configuration, ui } = useAppState();
  const [section, setSection] = useState<string>("general");
  const [query, setQuery] = useState("");
  const [pendingSection, setPendingSection] = useState<string | null>(null);
  const feature = connection.features.includes("config.settings");

  const back = () => store.dispatch({ type: "ui/page", page: "chat" });

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") back();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // The sidebar lists config sections, so the full schema is needed even when
  // the user never opens a config page. Fetch the fields in the same read:
  // otherwise every section would wait for a second `config.get` after the
  // schema bootstrap, making the generic pages slower than LLM/Plugins.
  useEffect(() => {
    if (
      connection.status === "ready" &&
      feature &&
      !configuration.schemaComplete
    ) {
      controller.refreshConfig(undefined, true, true);
    }
  }, [connection.status, feature, configuration.schemaComplete]);

  const fixedSections: Array<{ id: FixedSectionId; label: string }> = [
    { id: "general", label: strings.settings.general },
    { id: "appearance", label: strings.settings.appearance },
    { id: "connection", label: strings.settings.connection },
    { id: "plugins", label: strings.settings.plugins },
    { id: "providers", label: strings.settings.providers },
  ];
  const needle = query.trim().toLowerCase();
  const visibleFixed = needle
    ? fixedSections.filter(({ label }) => label.toLowerCase().includes(needle))
    : fixedSections;

  const configSections = useMemo(
    () => buildConfigSections(configuration.schema),
    [configuration.schema],
  );
  const visibleConfig = useMemo(
    () => configSections.filter((candidate) =>
      sectionMatches(candidate, query, ui.locale),
    ),
    [configSections, query, ui.locale],
  );

  // The LLM (Providers) and Plugins pages are agent capabilities, so they sit
  // under the Agent group together with the agent.* schema sections.
  const CLIENT_FIXED: FixedSectionId[] = ["general", "appearance", "connection"];
  const AGENT_FIXED: FixedSectionId[] = ["providers", "plugins"];
  const inOrder = (ids: FixedSectionId[]) => ids.flatMap(
    (id) => visibleFixed.filter((item) => item.id === id),
  );
  const clientFixed = inOrder(CLIENT_FIXED);
  const agentFixed = inOrder(AGENT_FIXED);
  const agentConfig = visibleConfig.filter(
    (candidate) => groupOf(candidate) === "agent",
  );
  const otherConfig = visibleConfig.filter(
    (candidate) => groupOf(candidate) !== "agent",
  );

  const renderFixedItem = ({ id, label }: { id: FixedSectionId; label: string }) => {
    const Icon = FIXED_ICONS[id];
    return (
      <button
        key={id}
        className={`settings-nav-item${section === id ? " active" : ""}`}
        onClick={() => requestSection(id)}
        aria-current={section === id ? "page" : undefined}
      >
        <Icon size={16} />
        <span>{label}</span>
      </button>
    );
  };

  const renderConfigItem = (
    candidate: (typeof configSections)[number],
  ) => (
    <button
      key={candidate.key}
      className={`settings-nav-item${
        section === candidate.key ? " active" : ""
      }`}
      onClick={() => requestSection(candidate.key)}
      aria-current={section === candidate.key ? "page" : undefined}
    >
      <SlidersHorizontal size={16} />
      <span>{sectionTitle(candidate, ui.locale)}</span>
    </button>
  );

  const isConfigSection = (id: string) =>
    configSections.some((candidate) => candidate.key === id);
  const activeKnown =
    fixedSections.some(({ id }) => id === section) ||
    isConfigSection(section);

  useEffect(() => {
    if (!activeKnown) setSection("general");
  }, [activeKnown]);

  const requestSection = (id: string) => {
    if (id === section) return;
    if (isConfigSection(section) && configuration.dirty) {
      setPendingSection(id);
      return;
    }
    store.dispatch({ type: "configuration/previewCleared" });
    setSection(id);
  };

  const confirmSwitch = () => {
    const target = pendingSection;
    if (!target) return;
    if (isConfigSection(section)) {
      store.dispatch({
        type: "intent/configDraft",
        section,
        dirty: false,
      });
    }
    store.dispatch({ type: "configuration/previewCleared" });
    setSection(target);
    setPendingSection(null);
  };

  return (
    <div className="settings-page">
      <header className="settings-bar">
        <button className="btn btn-ghost btn-sm" onClick={back}>
          <ArrowLeft size={15} /> {strings.settings.back}
        </button>
        <SettingsIcon className="settings-bar-icon" size={17} />
        <span className="settings-bar-title">{strings.nav.settings}</span>
        <div className="settings-bar-spacer" />
        <div className="topbar-status" title={connection.url}>
          <span className={`status-dot ${connection.status}`} />
          <span className="truncate">{strings.status[connection.status]}</span>
        </div>
      </header>

      <aside className="settings-nav" aria-label={strings.nav.settings}>
        <label className="settings-search">
          <Search size={14} />
          <input
            className="input"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={strings.settings.searchAll}
            aria-label={strings.settings.searchAll}
          />
        </label>

        <div className="settings-nav-scroll">
          {clientFixed.length > 0 ? (
            <>
              <p className="settings-nav-group">
                {strings.settings.groupClient}
              </p>
              {clientFixed.map(renderFixedItem)}
            </>
          ) : null}

          {agentFixed.length > 0 || agentConfig.length > 0 ? (
            <>
              <p className="settings-nav-group">
                {groupLabel("agent", strings)}
              </p>
              {agentFixed.map(renderFixedItem)}
              {agentConfig.map(renderConfigItem)}
            </>
          ) : null}

          {otherConfig.map((candidate, index) => {
            const previous = otherConfig[index - 1];
            const showGroup =
              !previous || groupOf(previous) !== groupOf(candidate);
            return (
              <div key={candidate.key}>
                {showGroup ? (
                  <p className="settings-nav-group">
                    {groupLabel(groupOf(candidate), strings)}
                  </p>
                ) : null}
                {renderConfigItem(candidate)}
              </div>
            );
          })}

          {visibleFixed.length === 0 && visibleConfig.length === 0 ? (
            <p className="small muted settings-nav-empty">
              {strings.settings.configSearchEmpty}
            </p>
          ) : null}
        </div>
      </aside>

      <main className="settings-main">
        <div className="settings-main-inner">
          {section === "general" ? <GeneralSection locale={ui.locale} /> : null}
          {section === "appearance" ? <AppearanceSection theme={ui.theme} /> : null}
          {section === "connection" ? <ConnectionSection /> : null}
          {section === "plugins" ? <PluginsSection /> : null}
          {section === "providers" ? <ProvidersSection /> : null}
          {isConfigSection(section) ? (
            <ConfigSettingsSection sectionKey={section} query={query} />
          ) : null}
        </div>
      </main>

      {pendingSection ? (
        <Dialog
          title={strings.settings.configDiscard}
          onClose={() => setPendingSection(null)}
          footer={
            <>
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => setPendingSection(null)}
              >
                {strings.settings.configSecretCancel}
              </button>
              <button
                type="button"
                className="btn btn-danger btn-sm"
                onClick={confirmSwitch}
              >
                {strings.settings.configDiscard}
              </button>
            </>
          }
        >
          <p>{strings.settings.configDiscardConfirm}</p>
        </Dialog>
      ) : null}
    </div>
  );
}
