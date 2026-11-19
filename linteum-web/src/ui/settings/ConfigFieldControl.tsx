/**
 * One schema-driven config field (web-config Phase 3 §6.5).
 *
 * Renders by `ConfigFieldSpec.type`; secrets use the three-state edit model and
 * composite values stay read-only. The control is prop-driven so it can be
 * rendered in tests without the gateway.
 */

import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";
import type { ReactElement, ReactNode } from "react";
import type {
  ConfigFieldSpec,
  ConfigFieldValue,
} from "../../protocol/commands";
import {
  configEditValue,
  fieldDescription,
  fieldTitle,
  type SecretEdit,
} from "../../state/configuration";
import type { Locale, Strings } from "../../strings";

const SOURCE_KEYS: Record<string, keyof Strings["settings"]> = {
  default: "configSourceDefault",
  file: "configSourceFile",
  env: "configSourceEnv",
  cli: "configSourceCli",
};

const SCOPE_KEYS: Record<string, keyof Strings["settings"]> = {
  live: "configScopeLive",
  next_connection: "configScopeNextConnection",
  new_session: "configScopeNewSession",
  restart: "configScopeRestart",
  client: "configScopeClient",
  noop: "configScopeNoop",
};

function controlId(spec: ConfigFieldSpec): string {
  return `cfg-${spec.key.replace(/[^A-Za-z0-9_-]/g, "-")}`;
}

function sourceLabel(strings: Strings, source: string): string {
  const key = SOURCE_KEYS[source];
  return key ? strings.settings[key] as string : source;
}

function scopeLabel(strings: Strings, scope: string): string {
  const key = SCOPE_KEYS[scope];
  return key ? strings.settings[key] as string : scope;
}

export function ConfigBadges({
  field,
  scope,
  strings,
}: {
  field?: ConfigFieldValue;
  scope: string;
  strings: Strings;
}): ReactElement {
  return (
    <span className="config-badges">
      {field ? (
        <span className={`badge badge-source-${field.source}`}>
          {sourceLabel(strings, field.source)}
        </span>
      ) : null}
      <span className={`badge badge-scope-${scope}`}>
        {scopeLabel(strings, scope)}
      </span>
    </span>
  );
}

function parseNumber(
  spec: ConfigFieldSpec,
  raw: string,
): number | null | undefined {
  if (raw === "") return spec.nullable ? null : undefined;
  const value = Number(raw);
  if (!Number.isFinite(value)) return undefined;
  return spec.type === "int" ? Math.trunc(value) : value;
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function ListFieldControl({
  spec,
  value,
  disabled,
  locale,
  strings,
  onChange,
}: {
  spec: ConfigFieldSpec;
  value: unknown;
  disabled: boolean;
  locale: Locale;
  strings: Strings;
  onChange(value: unknown): void;
}): ReactElement {
  const items: unknown[] = Array.isArray(value) ? value : [];
  const replace = (index: number, item: unknown) => {
    const next = [...items];
    next[index] = item;
    onChange(next);
  };
  const remove = (index: number) => {
    onChange(items.filter((_, current) => current !== index));
  };
  const move = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= items.length) return;
    const next = [...items];
    const [item] = next.splice(index, 1);
    next.splice(target, 0, item);
    onChange(next);
  };
  const itemType = spec.item_type || "str";
  return (
    <div className="config-list">
      {items.length === 0 ? (
        <p className="small muted">{strings.settings.configListEmpty}</p>
      ) : null}
      {items.map((item, index) => (
        <div className="config-list-row" key={index}>
          {itemType === "bool" ? (
            <select
              className="input"
              value={item === true ? "true" : "false"}
              disabled={disabled}
              onChange={(event) =>
                replace(index, event.target.value === "true")
              }
            >
              <option value="true">true</option>
              <option value="false">false</option>
            </select>
          ) : (
            <input
              className="input"
              type={itemType === "int" || itemType === "float" ? "number" : "text"}
              value={displayValue(item)}
              disabled={disabled}
              onChange={(event) => {
                if (itemType === "int" || itemType === "float") {
                  const number = Number(event.target.value);
                  replace(
                    index,
                    Number.isFinite(number)
                      ? itemType === "int"
                        ? Math.trunc(number)
                        : number
                      : "",
                  );
                } else {
                  replace(index, event.target.value);
                }
              }}
            />
          )}
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={disabled}
            onClick={() => move(index, -1)}
            aria-label={strings.settings.fallbackMoveUp}
          >
            <ArrowUp size={13} />
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={disabled}
            onClick={() => move(index, 1)}
            aria-label={strings.settings.fallbackMoveDown}
          >
            <ArrowDown size={13} />
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={disabled}
            onClick={() => remove(index)}
            aria-label={strings.settings.configListRemove}
          >
            <Trash2 size={13} />
          </button>
        </div>
      ))}
      <button
        type="button"
        className="btn btn-sm"
        disabled={disabled}
        onClick={() => onChange([...items, itemType === "bool" ? false : ""])}
      >
        <Plus size={13} /> {strings.settings.configListAdd}
      </button>
    </div>
  );
}

function SecretControl({
  spec,
  field,
  locale,
  strings,
  disabled,
  edit,
  onEdit,
}: {
  spec: ConfigFieldSpec;
  field?: ConfigFieldValue;
  locale: Locale;
  strings: Strings;
  disabled: boolean;
  edit: SecretEdit;
  onEdit(edit: SecretEdit): void;
}): ReactElement {
  const id = controlId(spec);
  if (disabled) {
    return (
      <input
        className="input"
        id={id}
        type="password"
        value={field?.masked ?? ""}
        disabled
        readOnly
      />
    );
  }
  if (edit.mode === "replace") {
    return (
      <div className="config-secret-edit">
        <input
          className="input"
          id={id}
          type="password"
          autoComplete="new-password"
          value={edit.value}
          placeholder={field?.masked ?? strings.settings.configSecretSet}
          onChange={(event) => onEdit({ mode: "replace", value: event.target.value })}
        />
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => onEdit({ mode: "unchanged" })}
        >
          {strings.settings.configSecretCancel}
        </button>
      </div>
    );
  }
  if (edit.mode === "clear") {
    return (
      <div className="config-secret-edit">
        <span className="small danger">{strings.settings.configSecretWillClear}</span>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => onEdit({ mode: "unchanged" })}
        >
          {strings.settings.configSecretCancel}
        </button>
      </div>
    );
  }
  return (
    <div className="config-secret-edit">
      <input
        className="input"
        id={id}
        type="password"
        value=""
        readOnly
        placeholder={
          field?.set
            ? field.masked ?? strings.settings.configSecretSet
            : strings.settings.providerNone
        }
      />
      <button
        type="button"
        className="btn btn-ghost btn-sm"
        onClick={() => onEdit({ mode: "replace", value: "" })}
      >
        {strings.settings.configSecretReplace}
      </button>
      {spec.nullable && field?.set ? (
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => onEdit({ mode: "clear" })}
        >
          {strings.settings.configSecretClear}
        </button>
      ) : null}
    </div>
  );
}

export interface ConfigFieldControlProps {
  spec: ConfigFieldSpec;
  field?: ConfigFieldValue;
  value: unknown;
  locale: Locale;
  strings: Strings;
  scope: string;
  disabled?: boolean;
  error?: string | null;
  secretEdit?: SecretEdit;
  onChange(value: unknown): void;
  onSecretEdit?(edit: SecretEdit): void;
  onReset?(): void;
  resetDisabled?: boolean;
}

export function ConfigFieldControl({
  spec,
  field,
  value,
  locale,
  strings,
  scope,
  disabled = false,
  error = null,
  secretEdit = { mode: "unchanged" },
  onChange,
  onSecretEdit,
  onReset,
  resetDisabled = false,
}: ConfigFieldControlProps): ReactElement {
  const id = controlId(spec);
  const envLocked = field?.source === "env";
  const locked = disabled || envLocked;
  const title = fieldTitle(spec, locale);
  const description = fieldDescription(spec, locale);
  const editValue = value === undefined ? configEditValue(field) : value;
  const raw = field?.raw_value;
  const showRaw =
    raw !== undefined &&
    raw !== null &&
    JSON.stringify(raw) !== JSON.stringify(field?.value);

  const wide =
    spec.secret ||
    spec.type === "list" ||
    spec.type === "map" ||
    spec.type === "object" ||
    (description?.length ?? 0) > 80;

  let control: ReactNode = null;
  if (spec.secret) {
    control = (
      <SecretControl
        spec={spec}
        field={field}
        locale={locale}
        strings={strings}
        disabled={locked}
        edit={secretEdit}
        onEdit={onSecretEdit ?? (() => undefined)}
      />
    );
  } else if (spec.type === "map" || spec.type === "object" ||
             (spec.type === "list" && spec.item_type === "object")) {
    return (
      <div className="settings-field config-field config-field-wide">
        <div className="config-field-head">
          <label className="label">{title}</label>
          <ConfigBadges field={field} scope={scope} strings={strings} />
        </div>
        {description ? <p className="small muted">{description}</p> : null}
        <pre className="settings-readonly">
          {JSON.stringify(field?.value ?? spec.default, null, 2)}
        </pre>
        <p className="small muted">{strings.settings.configCompositeReadonly}</p>
      </div>
    );
  } else if (spec.type === "list") {
    control = (
      <ListFieldControl
        spec={spec}
        value={editValue}
        disabled={locked}
        locale={locale}
        strings={strings}
        onChange={onChange}
      />
    );
  } else if (spec.type === "bool") {
    if (spec.nullable) {
      control = (
        <select
          className="input"
          id={id}
          value={editValue === null ? "null" : editValue ? "true" : "false"}
          disabled={locked}
          onChange={(event) => {
            const next = event.target.value;
            onChange(next === "null" ? null : next === "true");
          }}
        >
          <option value="null">{strings.settings.configSourceDefault}</option>
          <option value="true">true</option>
          <option value="false">false</option>
        </select>
      );
    } else {
      control = (
        <label className="config-checkbox">
          <input
            id={id}
            type="checkbox"
            checked={editValue === true}
            disabled={locked}
            onChange={(event) => onChange(event.target.checked)}
          />
          <span>{editValue === true ? "true" : "false"}</span>
        </label>
      );
    }
  } else if (spec.type === "enum") {
    if (spec.allow_custom) {
      control = (
        <>
          <input
            className="input"
            id={id}
            list={`${id}-options`}
            value={displayValue(editValue)}
            disabled={locked}
            onChange={(event) => onChange(event.target.value)}
          />
          <datalist id={`${id}-options`}>
            {spec.enum.map((option) => (
              <option key={option} value={option} />
            ))}
          </datalist>
        </>
      );
    } else {
      control = (
        <select
          className="input"
          id={id}
          value={editValue === null ? "" : displayValue(editValue)}
          disabled={locked}
          onChange={(event) => {
            const next = event.target.value;
            onChange(next === "" && spec.nullable ? null : next);
          }}
        >
          {spec.nullable ? (
            <option value="">{strings.settings.configSourceDefault}</option>
          ) : null}
          {spec.enum.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      );
    }
  } else if (spec.type === "int" || spec.type === "float") {
    control = (
      <input
        className="input"
        id={id}
        type="number"
        inputMode={spec.type === "int" ? "numeric" : "decimal"}
        step={spec.type === "float" ? "any" : "1"}
        min={spec.minimum ?? undefined}
        max={spec.maximum ?? undefined}
        value={editValue === null || editValue === undefined ? "" : String(editValue)}
        disabled={locked}
        onChange={(event) => onChange(parseNumber(spec, event.target.value))}
      />
    );
  } else {
    control = (
      <input
        className="input"
        id={id}
        type="text"
        value={displayValue(editValue)}
        disabled={locked}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  }

  return (
    <div
      className={`settings-field config-field${
        wide ? " config-field-wide" : ""
      }${error ? " has-error" : ""}`}
    >
      <div className="config-field-head">
        <label className="label" htmlFor={id}>
          {title}
        </label>
        <span className="config-field-side">
          <ConfigBadges field={field} scope={scope} strings={strings} />
          {onReset ? (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              disabled={resetDisabled}
              onClick={onReset}
            >
              {strings.settings.configResetField}
            </button>
          ) : null}
        </span>
      </div>
      {description ? <p className="small muted">{description}</p> : null}
      {control}
      {showRaw ? (
        <p className="small muted config-raw-hint">
          {strings.settings.configRawValueHint
            .replace("{raw}", displayValue(raw))
            .replace("{resolved}", displayValue(field?.value))}
        </p>
      ) : null}
      {envLocked ? (
        <p className="small muted">
          {strings.settings.configEnvLockedBy.replace(
            "{envVar}",
            field?.env_var ?? "env",
          )}
        </p>
      ) : null}
      {error ? <p className="small danger">{error}</p> : null}
    </div>
  );
}
