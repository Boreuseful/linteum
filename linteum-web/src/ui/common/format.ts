/**
 * Display label for a mode: builtin modes use their bare name; plugin modes
 * are qualified as `plugin.<name>` so they cannot be mistaken for builtins.
 */
export function modeLabel(mode: { name: string; source?: string | null }): string {
  return mode.source?.startsWith("plugin") ? `plugin.${mode.name}` : mode.name;
}

export function formatDuration(ms?: number | null): string {
  if (ms === undefined || ms === null || ms < 0) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

export function formatTokens(value?: number | null): string {
  if (value === undefined || value === null || Number.isNaN(value)) return "—";
  if (value < 1000) return String(Math.round(value));
  if (value < 1_000_000) return `${(value / 1000).toFixed(1)}k`;
  return `${(value / 1_000_000).toFixed(2)}M`;
}

export function formatBytes(value?: number | null): string {
  if (value === undefined || value === null) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatClock(ts?: number): string {
  if (!ts) return "";
  try {
    return new Date(ts).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}

/** `SessionInfo.updated_at` is Unix seconds; return a compact relative label. */
export function relativeTime(seconds?: number): string {
  if (!seconds) return "";
  const diff = Math.max(0, Date.now() / 1000 - seconds);
  if (diff < 60) return "now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86_400) return `${Math.floor(diff / 3600)}h`;
  if (diff < 604_800) return `${Math.floor(diff / 86_400)}d`;
  try {
    return new Date(seconds * 1000).toLocaleDateString();
  } catch {
    return "";
  }
}

export function outputText(output: unknown): string {
  if (output === undefined || output === null) return "";
  if (typeof output === "string") return output;
  try {
    return JSON.stringify(output, null, 2);
  } catch {
    return String(output);
  }
}

export function argsText(args: unknown): string {
  return outputText(args ?? {});
}

/** One-line JSON for summaries (no pretty-print newlines). */
export function compactValue(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max)}…`;
}

export function firstLine(text: string, max = 96): string {
  const line = text.split("\n").find((entry) => entry.trim() !== "") ?? "";
  return truncate(line.trim(), max);
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** Triggers a client-side download for an in-memory blob. */
export function downloadBlob(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.hidden = true;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function usageTotal(input?: number, output?: number): number {
  return (input ?? 0) + (output ?? 0);
}
