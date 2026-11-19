import { useState } from "react";
import { useStrings } from "../../hooks/useStrings";
import type { Strings } from "../../strings";
import {
  presentToolArgs,
  presentToolResult,
  rawArgsText,
  type ToolField,
  type ToolFieldKey,
} from "./toolPresentation";

const LABEL_KEYS: Record<ToolFieldKey, keyof Strings["chat"]> = {
  command: "toolCommand",
  path: "toolPath",
  content: "toolContent",
  old: "toolOld",
  new: "toolNew",
  cwd: "toolCwd",
  directory: "toolDirectory",
  stdout: "toolStdout",
  stderr: "toolStderr",
  fileContent: "toolFileContent",
};

function fieldLabel(strings: Strings, key: ToolFieldKey): string {
  return strings.chat[LABEL_KEYS[key]];
}

function isPathField(key: ToolFieldKey): boolean {
  return key === "path" || key === "cwd" || key === "directory";
}

function ToolFieldView({ field, label }: { field: ToolField; label: string }) {
  return (
    <div className="panel-section">
      <span className="tool-section-label">{label}</span>
      {isPathField(field.key) ? (
        <code className="tool-path">{field.value}</code>
      ) : (
        <pre className="tool-pre">{field.value}</pre>
      )}
    </div>
  );
}

/**
 * Spec rendering for a tool's arguments: labelled fields for the built-in
 * workspace tools, with the untouched JSON one click away. Unknown tools fall
 * straight through to the raw JSON (the previous behaviour).
 */
export function ToolInvocationView({
  name,
  args,
  rawText,
}: {
  name: string;
  args: unknown;
  /** Original text to reveal behind the toggle; defaults to pretty args JSON. */
  rawText?: string;
}) {
  const strings = useStrings();
  const [showRaw, setShowRaw] = useState(false);
  const presentation = presentToolArgs(name, args);
  const raw = rawText ?? rawArgsText(args);

  if (!presentation.structured) {
    return raw ? <pre className="tool-pre">{raw}</pre> : null;
  }

  return (
    <div className="tool-invocation">
      {presentation.fields.map((field, index) => (
        <ToolFieldView
          key={`${field.key}-${index}`}
          field={field}
          label={fieldLabel(strings, field.key)}
        />
      ))}
      <div className="tool-actions">
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => setShowRaw((value) => !value)}
        >
          {showRaw ? strings.chat.hideRaw : strings.chat.showRaw}
        </button>
      </div>
      {showRaw ? <pre className="tool-pre">{raw}</pre> : null}
    </div>
  );
}

/** Spec rendering for a tool's result (`stdout`/`stderr`, file contents, …). */
export function ToolResultView({
  name,
  output,
}: {
  name: string;
  output: unknown;
}) {
  const strings = useStrings();
  const presentation = presentToolResult(name, output);
  if (
    presentation === null ||
    (presentation.fields.length === 0 && presentation.exitCode === undefined)
  ) {
    return null;
  }

  return (
    <div className="tool-invocation">
      {presentation.exitCode !== undefined ? (
        <span className="tool-exit">
          {strings.chat.toolExitCode}: {presentation.exitCode}
        </span>
      ) : null}
      {presentation.fields.map((field, index) => (
        <ToolFieldView
          key={`${field.key}-${index}`}
          field={field}
          label={fieldLabel(strings, field.key)}
        />
      ))}
    </div>
  );
}
