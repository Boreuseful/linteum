/**
 * Gateway-wide configuration state and draft helpers (web-config §8.3).
 *
 * The reducer never talks to the transport; components keep a local draft and
 * this module turns it into the minimal `changes` map sent to `config.set`.
 */

import type {
  ConfigFieldSpec,
  ConfigFieldValue,
  ConfigIssue,
  ConfigSchema,
  ConfigSchemaSection,
  ConfigSetResult,
} from "../protocol/commands";
import type { ErrorInfo } from "../protocol/envelope";
import type { ConfigChangedKey } from "../protocol/frames";

/** Per-secret edit state; the plaintext is never mirrored from the gateway. */
export type SecretEdit =
  | { mode: "unchanged" }
  | { mode: "replace"; value: string }
  | { mode: "clear" };

export interface SectionLoadState {
  loading: boolean;
  error: ErrorInfo | null;
  loadedAtRevision: string;
}

export interface ConfigurationState {
  schema: ConfigSchema | null;
  fields: Record<string, ConfigFieldValue>;
  issues: ConfigIssue[];
  revision: string;
  path: string;
  exists: boolean;
  manage: boolean;
  writable: boolean;
  runtimeScopes: Record<string, string>;
  /** True only after an unfiltered schema response; scoped loads do not set it. */
  schemaComplete: boolean;
  loadedSections: Record<string, SectionLoadState>;
  /** Per-section unsaved draft flags; ``dirty`` is the aggregate. */
  draftSections: Record<string, boolean>;
  dirty: boolean;
  /** Latest revision announced by ``config.changed`` (null = none). */
  remoteRevision: string | null;
  /** Keys changed by that push; drives the "view changes" link. */
  remoteChangedKeys: ConfigChangedKey[];
  /** Monotonic counter; identifies a read that started before a push. */
  remoteEpoch: number;
  preview: ConfigSetResult | null;
  previewing: boolean;
  previewRevisionMismatch: boolean;
  /** ``config.get`` succeeded but issues contain an error (repairable). */
  configInvalid: boolean;
  loading: boolean;
  saving: boolean;
  error: ErrorInfo | null;
  lastSave: ConfigSetResult | null;
  conflict: { currentRevision: string | null } | null;
}

export const INITIAL_CONFIGURATION: ConfigurationState = {
  schema: null,
  fields: {},
  issues: [],
  revision: "",
  path: "",
  exists: false,
  manage: false,
  writable: false,
  runtimeScopes: {},
  schemaComplete: false,
  loadedSections: {},
  draftSections: {},
  dirty: false,
  remoteRevision: null,
  remoteChangedKeys: [],
  remoteEpoch: 0,
  preview: null,
  previewing: false,
  previewRevisionMismatch: false,
  configInvalid: false,
  loading: false,
  saving: false,
  error: null,
  lastSave: null,
  conflict: null,
};

/** True when a push announced a revision the client has not loaded yet. */
export function configRemoteStale(configuration: ConfigurationState): boolean {
  return (
    configuration.remoteRevision !== null &&
    configuration.remoteRevision !== configuration.revision
  );
}

/** Sections a stale-banner reload must refresh (§6.5.4). */
export function configReloadSections(
  configuration: ConfigurationState,
  fallback: string[],
): string[] {
  const loaded = Object.keys(configuration.loadedSections);
  return loaded.length > 0 ? loaded : [...fallback];
}

/** The editor states Providers reports as a configuration draft (§7.4). */
export interface ConfigEditorDraftInput {
  kind: "profile" | "new-profile" | "duplicate" | "fallback" | "new-fallback";
  name?: string;
  index?: number;
  from?: string;
}

/**
 * Section-aware draft state for the Providers editor.
 *
 * `null` means "this editor has no config draft" (closed, or the duplicate
 * dialog); otherwise the caller dispatches `intent/configDraft` so the global
 * `dirty` flag blocks auto-refresh while the user is typing.
 */
export function configEditorDraft(
  editor: ConfigEditorDraftInput | null,
  fields: Record<string, ConfigFieldValue>,
  draft: Record<string, unknown>,
  secrets: Record<string, SecretEdit>,
): { section: "agent.llm" | "agent.profiles"; dirty: boolean } | null {
  if (!editor || editor.kind === "duplicate") return null;
  const existing = editor.kind === "profile" && editor.name
    ? profileValues(fields, editor.name)
    : editor.kind === "fallback" && typeof editor.index === "number"
      ? fallbackValues(fields, editor.index)
      : {};
  const isFallback = editor.kind === "fallback" || editor.kind === "new-fallback";
  const changes = isFallback
    ? collectFallbackChanges(draft, existing, secrets)
    : collectProfileChanges(draft, existing, secrets);
  return {
    section: editor.kind === "profile" && editor.name === "default"
      ? "agent.llm"
      : "agent.profiles",
    dirty: Object.keys(changes).length > 0,
  };
}

/** LLM profile leaves; the `default` profile is the `agent.llm.*` section. */
export const PROFILE_FIELDS = [
  "provider",
  "model",
  "openai_mode",
  "api_key",
  "base_url",
  "timeout",
  "temperature",
  "max_tokens",
  "top_p",
  "reasoning_effort",
  "thinking",
] as const;

/** Names a profile may never take: `default` and the secret leaf names. */
const RESERVED_PROFILE_NAMES = new Set([
  "default",
  "api_key",
  "password",
  "secret",
  "token",
]);

const PROFILE_NAME_PATTERN = /^[A-Za-z0-9._-]{1,64}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** True when the New Session dialog still needs the profile catalogue. */
export function needsProfileFields(state: ConfigurationState): boolean {
  return (
    state.fields["agent.profiles"] === undefined ||
    state.fields["agent.default_profile"] === undefined
  );
}

/** The value a form should edit: file-level raw value wins over resolved. */
export function configEditValue(field: ConfigFieldValue | undefined): unknown {
  if (!field) return undefined;
  return field.raw_value ?? field.value;
}

/** Minimal client-side checks; the gateway remains authoritative. */
export function validateConfigChanges(
  specs: readonly ConfigFieldSpec[],
  changes: Record<string, unknown>,
): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const spec of specs) {
    if (!(spec.key in changes)) continue;
    const error = validateConfigValue(spec, changes[spec.key]);
    if (error) errors[spec.key] = error;
  }
  return errors;
}

function validateConfigValue(spec: ConfigFieldSpec, value: unknown): string | null {
  if (value === undefined || value === null) {
    return spec.nullable ? null : "configFieldRequired";
  }
  if (spec.type === "int" || spec.type === "float") {
    if (
      typeof value !== "number" ||
      !Number.isFinite(value) ||
      (spec.type === "int" && !Number.isInteger(value))
    ) {
      return "configFieldInvalid";
    }
    if (spec.minimum !== null && value < spec.minimum) return "configFieldRange";
    if (spec.maximum !== null && value > spec.maximum) return "configFieldRange";
    return null;
  }
  if (spec.type === "bool") {
    return typeof value === "boolean" ? null : "configFieldInvalid";
  }
  if (spec.type === "enum") {
    if (typeof value !== "string") return "configFieldInvalid";
    if (!spec.allow_custom && !spec.enum.includes(value)) return "configFieldEnum";
    return null;
  }
  if (spec.type === "list") {
    if (!Array.isArray(value)) return "configFieldListType";
    if (value.length > 256) return "configFieldListLimit";
    const itemType = spec.item_type || "str";
    for (const item of value) {
      if (itemType === "bool" && typeof item !== "boolean") {
        return "configFieldListType";
      }
      if (itemType === "int" && (typeof item !== "number" || !Number.isInteger(item))) {
        return "configFieldListType";
      }
      if (itemType === "float" && (typeof item !== "number" || !Number.isFinite(item))) {
        return "configFieldListType";
      }
      if ((itemType === "str" || itemType === "path") && typeof item !== "string") {
        return "configFieldListType";
      }
      if (itemType === "object" && (
        typeof item !== "object" || item === null || Array.isArray(item)
      )) {
        return "configFieldListType";
      }
    }
    return null;
  }
  if (spec.type === "str" || spec.type === "path" || spec.type === "secret") {
    if (typeof value !== "string") return "configFieldInvalid";
    if (!spec.nullable && value.trim() === "") return "configFieldRequired";
    return null;
  }
  return null;
}

function sameValue(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  try {
    return JSON.stringify(left) === JSON.stringify(right);
  } catch {
    return false;
  }
}

/** Locale-aware schema text with a fallback to the canonical value. */
export function fieldTitle(spec: ConfigFieldSpec, locale: string): string {
  return spec.title_i18n?.[locale] || spec.title;
}

export function fieldDescription(spec: ConfigFieldSpec, locale: string): string {
  return spec.description_i18n?.[locale] || spec.description;
}

export function sectionTitle(section: ConfigSchemaSection, locale: string): string {
  return section.title_i18n?.[locale] || section.title;
}

/**
 * The section a dotted key belongs to, using the schema when available.
 * Dynamic profile/fallback leaves collapse to the composite section.
 */
export function configKeySection(
  key: string,
  schema: ConfigSchema | null,
): string {
  if (schema) {
    for (const section of schema.sections) {
      if (section.fields.some((spec) => spec.key === key)) return section.key;
    }
  }
  if (key.startsWith("agent.profiles.")) return "agent.profiles";
  if (key.startsWith("agent.fallbacks.")) return "agent.profiles";
  const index = key.lastIndexOf(".");
  return index > 0 ? key.slice(0, index) : key;
}

/** Default profile name plus the sorted named-profile catalogue. */
export function profileCatalog(fields: Record<string, ConfigFieldValue>): {
  defaultProfile: string;
  names: string[];
} {
  const defaultProfile = fields["agent.default_profile"]?.value;
  const profiles = fields["agent.profiles"]?.value;
  return {
    defaultProfile: typeof defaultProfile === "string" ? defaultProfile : "default",
    names: isRecord(profiles) ? Object.keys(profiles).sort() : [],
  };
}

/** Values for the `default` (`agent.llm.*`) or a named profile. */
export function profileValues(
  fields: Record<string, ConfigFieldValue>,
  name: string,
): Record<string, unknown> {
  if (name === "default") {
    const values: Record<string, unknown> = {};
    for (const field of PROFILE_FIELDS) {
      values[field] = fields[`agent.llm.${field}`]?.value;
    }
    return values;
  }
  const profiles = fields["agent.profiles"]?.value;
  if (!isRecord(profiles)) return {};
  const entry = profiles[name];
  return isRecord(entry) ? { ...entry } : {};
}

/** Values for one fallback entry in `agent.fallbacks`. */
export function fallbackValues(
  fields: Record<string, ConfigFieldValue>,
  index: number,
): Record<string, unknown> {
  const fallbacks = fields["agent.fallbacks"]?.value;
  if (!Array.isArray(fallbacks)) return {};
  const entry = fallbacks[index];
  return isRecord(entry) ? { ...entry } : {};
}

/**
 * Compare a local draft with the gateway's values and return only dirty keys.
 *
 * Secrets are handled separately: `replace` sends the new string (unless it is
 * empty), `clear` sends `null`, and `unchanged` is omitted entirely.
 */
export function collectConfigChanges(
  draft: Record<string, unknown>,
  fields: Record<string, ConfigFieldValue>,
  secrets: Record<string, SecretEdit>,
): Record<string, unknown> {
  const changes: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(draft)) {
    if (value === undefined) continue;
    const current = fields[key];
    if (!current || sameValue(value, configEditValue(current))) continue;
    changes[key] = value;
  }
  for (const [key, edit] of Object.entries(secrets)) {
    if (edit.mode === "replace" && edit.value !== "") {
      changes[key] = edit.value;
    } else if (edit.mode === "clear") {
      changes[key] = null;
    }
  }
  return changes;
}

function collectEntryChanges(
  draft: Record<string, unknown>,
  existing: Record<string, unknown>,
  secrets: Record<string, SecretEdit>,
): Record<string, unknown> {
  const changes: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(draft)) {
    if (Object.is(value, existing[key])) continue;
    changes[key] = value;
  }
  for (const [key, edit] of Object.entries(secrets)) {
    if (edit.mode === "replace" && edit.value !== "") {
      changes[key] = edit.value;
    } else if (edit.mode === "clear") {
      changes[key] = null;
    }
  }
  return changes;
}

/** Dirty profile fields plus secret edits, ready for `config.profile.set`. */
export function collectProfileChanges(
  draft: Record<string, unknown>,
  existing: Record<string, unknown>,
  secrets: Record<string, SecretEdit>,
): Record<string, unknown> {
  return collectEntryChanges(draft, existing, secrets);
}

/** Dirty fallback fields plus secret edits, ready for `config.fallback.set`. */
export function collectFallbackChanges(
  draft: Record<string, unknown>,
  existing: Record<string, unknown>,
  secrets: Record<string, SecretEdit>,
): Record<string, unknown> {
  return collectEntryChanges(draft, existing, secrets);
}

/** Validate a new/renamed profile name before offering a save. */
export function profileNameError(
  name: string,
  names: readonly string[],
): "invalid" | "reserved" | "exists" | null {
  if (!PROFILE_NAME_PATTERN.test(name)) return "invalid";
  if (name.split(".").some((segment) => segment === "")) return "invalid";
  if (RESERVED_PROFILE_NAMES.has(name)) return "reserved";
  if (names.includes(name)) return "exists";
  return null;
}

/** The field a save/validation error points at, when the gateway supplied one. */
export function configErrorKey(error: ErrorInfo | null): string | null {
  const key = error?.context?.key;
  return typeof key === "string" ? key : null;
}

/** A broken/unparseable file makes the whole form read-only, even when stale. */
export function configReadBlocked(error: ErrorInfo | null): boolean {
  return (
    error?.code === "E_CONFIG_PARSE" ||
    error?.code === "E_CONFIG_IO" ||
    error?.code === "E_CONFIG_TYPE" ||
    error?.code === "E_CONFIG_UNKNOWN"
  );
}
