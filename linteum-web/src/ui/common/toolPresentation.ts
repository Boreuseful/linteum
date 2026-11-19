/**
 * Human-readable presentation for tool-call arguments and results.
 *
 * The gateway reports raw JSON for every tool. This pure layer recognises the
 * built-in workspace tools and turns their JSON into labelled fields a person
 * can scan (a bash command, a file path, the written text, …). Unknown tools
 * keep the generic JSON rendering. Nothing here is lossy: callers still hold
 * the original arguments for a raw-JSON fallback.
 */

import { compactValue, firstLine, outputText } from "./format";

export type ToolFieldKey =
  | "command"
  | "path"
  | "content"
  | "old"
  | "new"
  | "cwd"
  | "directory"
  | "stdout"
  | "stderr"
  | "fileContent";

export interface ToolField {
  key: ToolFieldKey;
  value: string;
}

export interface ToolArgsPresentation {
  /** False when the tool has no spec renderer (or its arguments are malformed). */
  structured: boolean;
  /** One-line label for the collapsed header. */
  summary: string;
  fields: ToolField[];
}

export interface ToolResultPresentation {
  structured: boolean;
  fields: ToolField[];
  /** Only present for a non-zero bash exit code. */
  exitCode?: number;
}

export interface ArgsPreview {
  args: unknown;
  raw: string;
  parsed: boolean;
}

type ToolKind = "bash" | "read_file" | "write_file" | "edit_file" | "list_dir";

/** Gateway-side `_preview_value` keys worth salvaging from a truncated payload. */
const ARG_KEYS = ["path", "command", "text", "old", "new", "cwd"] as const;
const RESULT_KEYS = ["returncode", "stdout", "stderr", "cwd"] as const;

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function textField(record: Record<string, unknown> | null, key: string): string | null {
  const value = record?.[key];
  return typeof value === "string" ? value : null;
}

/**
 * The gateway replaces payloads larger than 2 KB with
 * `{truncated: true, preview: "<first N chars of the JSON>"}`. Return the
 * preview text when that wrapper is present.
 */
function previewWrapper(value: unknown): string | null {
  const record = asRecord(value);
  return record?.truncated === true && typeof record.preview === "string"
    ? record.preview
    : null;
}

function decodePartialString(raw: string): string {
  try {
    return JSON.parse(`"${raw}"`) as string;
  } catch {
    try {
      // A cut in the middle of an escape leaves a dangling backslash.
      return JSON.parse(`"${raw.replace(/\\$/, "")}"`) as string;
    } catch {
      return raw;
    }
  }
}

function partialString(text: string, key: string): string | null {
  const pattern = new RegExp(`"${key}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)`);
  const match = pattern.exec(text);
  return match ? decodePartialString(match[1]) : null;
}

function partialNumber(text: string, key: string): number | null {
  const pattern = new RegExp(`"${key}"\\s*:\\s*(-?\\d+(?:\\.\\d+)?)`);
  const match = pattern.exec(text);
  if (!match) return null;
  const value = Number(match[1]);
  return Number.isFinite(value) ? value : null;
}

/** Recover the readable fields that survived a truncated JSON preview. */
function parsePartialPreview(
  text: string,
  keys: readonly string[],
): Record<string, unknown> {
  const record: Record<string, unknown> = {};
  for (const key of keys) {
    const stringValue = partialString(text, key);
    if (stringValue !== null) {
      record[key] = stringValue;
    } else {
      const numberValue = partialNumber(text, key);
      if (numberValue !== null) record[key] = numberValue;
    }
  }
  return record;
}

function unwrapArgs(args: unknown): {
  record: Record<string, unknown> | null;
  partial: boolean;
} {
  const preview = previewWrapper(args);
  if (preview !== null) {
    return { record: parsePartialPreview(preview, ARG_KEYS), partial: true };
  }
  return { record: asRecord(args), partial: false };
}

function partialValue(key: ToolFieldKey, value: string, partial: boolean): string {
  if (!partial || key === "path" || key === "directory" || key === "cwd") {
    return value;
  }
  return `${value}…`;
}

/** Text for the raw-JSON toggle; a truncated wrapper shows its inner preview. */
export function rawArgsText(args: unknown): string {
  const preview = previewWrapper(args);
  return preview ?? outputText(args ?? {});
}

function toolKind(name: string): ToolKind | null {
  switch (name.trim().toLowerCase()) {
    case "bash":
      return "bash";
    case "read_file":
      return "read_file";
    case "write_file":
      return "write_file";
    case "edit_file":
      return "edit_file";
    case "list_dir":
      return "list_dir";
    default:
      return null;
  }
}

function fallback(args: unknown): ToolArgsPresentation {
  return {
    structured: false,
    summary: firstLine(previewWrapper(args) ?? compactValue(args)),
    fields: [],
  };
}

/** Recognise built-in tool arguments and return scan-friendly fields. */
export function presentToolArgs(name: string, args: unknown): ToolArgsPresentation {
  const kind = toolKind(name);
  const { record, partial } = unwrapArgs(args);

  if (kind === "bash") {
    const command = textField(record, "command");
    if (command !== null && command.trim() !== "") {
      const fields: ToolField[] = [
        { key: "command", value: partialValue("command", command, partial) },
      ];
      const cwd = textField(record, "cwd");
      if (cwd !== null && cwd !== "" && cwd !== ".") {
        fields.push({ key: "cwd", value: cwd });
      }
      return {
        structured: true,
        summary: firstLine(partialValue("command", command, partial)),
        fields,
      };
    }
    return fallback(args);
  }

  if (kind === "read_file") {
    const path = textField(record, "path");
    if (path !== null) {
      return {
        structured: true,
        summary: path,
        fields: [{ key: "path", value: path }],
      };
    }
    return fallback(args);
  }

  if (kind === "write_file") {
    const path = textField(record, "path");
    if (path !== null) {
      const fields: ToolField[] = [{ key: "path", value: path }];
      const text = textField(record, "text");
      if (text !== null) {
        fields.push({
          key: "content",
          value: partialValue("content", text, partial),
        });
      }
      return { structured: true, summary: path, fields };
    }
    return fallback(args);
  }

  if (kind === "edit_file") {
    const path = textField(record, "path");
    if (path !== null) {
      const fields: ToolField[] = [{ key: "path", value: path }];
      const oldText = textField(record, "old");
      const newText = textField(record, "new");
      if (oldText !== null) {
        fields.push({ key: "old", value: partialValue("old", oldText, partial) });
      }
      if (newText !== null) {
        fields.push({ key: "new", value: partialValue("new", newText, partial) });
      }
      return { structured: true, summary: path, fields };
    }
    return fallback(args);
  }

  if (kind === "list_dir") {
    const path = textField(record, "path") ?? ".";
    return {
      structured: true,
      summary: path,
      fields: [{ key: "directory", value: path }],
    };
  }

  return fallback(args);
}

/** Recognise built-in tool results; `null` means "render the raw output". */
export function presentToolResult(
  name: string,
  output: unknown,
): ToolResultPresentation | null {
  const kind = toolKind(name);

  if (kind === "bash") {
    const preview = previewWrapper(output);
    const record =
      preview !== null
        ? parsePartialPreview(preview, RESULT_KEYS)
        : asRecord(output);
    if (record === null) return null;
    const partial = preview !== null;
    const stdout = textField(record, "stdout");
    const stderr = textField(record, "stderr");
    const returncode = typeof record.returncode === "number" ? record.returncode : null;
    if (stdout === null && stderr === null && returncode === null) return null;
    const fields: ToolField[] = [];
    if (stdout !== null && stdout !== "") {
      fields.push({ key: "stdout", value: partialValue("stdout", stdout, partial) });
    }
    if (stderr !== null && stderr !== "") {
      fields.push({ key: "stderr", value: partialValue("stderr", stderr, partial) });
    }
    return {
      structured: true,
      fields,
      ...(returncode !== null && returncode !== 0 ? { exitCode: returncode } : {}),
    };
  }

  if (kind === "read_file" && typeof output === "string") {
    return {
      structured: true,
      fields: [{ key: "fileContent", value: output }],
    };
  }

  return null;
}

/**
 * Approval frames carry `args_preview`, a redacted JSON string truncated by the
 * gateway. Parse it when intact; a truncated preview stays raw text.
 */
export function parseArgsPreview(preview: unknown): ArgsPreview {
  if (preview === undefined || preview === null) {
    return { args: undefined, raw: "", parsed: false };
  }
  if (typeof preview !== "string") {
    return { args: preview, raw: outputText(preview), parsed: true };
  }
  const trimmed = preview.trim();
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      return { args: JSON.parse(trimmed) as unknown, raw: preview, parsed: true };
    } catch {
      // Truncated or otherwise invalid JSON: keep the original text.
    }
  }
  return { args: undefined, raw: preview, parsed: false };
}
