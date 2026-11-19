/**
 * Resolve Markdown image/link references against a workspace file.
 *
 * External http(s) URLs stay untouched. Relative references are resolved from
 * the Markdown document's directory (root-relative `/...` from the workspace
 * root), normalised, and refused when they escape the workspace. Workspace
 * files are served by the provider's raw-bytes URL.
 */

export type MarkdownTarget =
  | { kind: "external"; url: string; external: boolean }
  | { kind: "workspace"; path: string; url: string; external: false };

export interface MarkdownTargetOptions {
  root: string;
  /** Absolute path of the Markdown file being previewed. */
  filePath: string;
  /** Images may not use `mailto:` or same-page anchors. */
  kind: "image" | "link";
  /** Provider hook that serves a workspace path as bytes; `null` = unavailable. */
  rawUrl: (root: string, path: string) => string | null;
}

const SCHEME = /^([a-z][a-z0-9+.-]*):/i;

/** Collapse `.`/`..` segments. Returns "" when a relative path underflows. */
function normalizeWorkspacePath(path: string): string {
  const absolute = path.startsWith("/");
  const parts: string[] = [];
  for (const part of path.split("/")) {
    if (part === "" || part === ".") continue;
    if (part === "..") {
      if (parts.length === 0) return "";
      parts.pop();
      continue;
    }
    parts.push(part);
  }
  return `${absolute ? "/" : ""}${parts.join("/")}`;
}

function directoryOf(path: string): string {
  const index = path.lastIndexOf("/");
  if (index < 0) return "";
  return index === 0 ? "/" : path.slice(0, index);
}

function isInside(root: string, path: string): boolean {
  const cleanRoot = root === "/" ? "/" : root.replace(/\/+$/, "");
  if (cleanRoot === "/") return path.startsWith("/");
  return path === cleanRoot || path.startsWith(`${cleanRoot}/`);
}

function decodePath(value: string): string | null {
  try {
    return decodeURIComponent(value);
  } catch {
    return null;
  }
}

export function resolveMarkdownTarget(
  reference: string,
  options: MarkdownTargetOptions,
): MarkdownTarget | null {
  const raw = reference.trim();
  if (!raw) return null;

  // Protocol-relative URLs inherit the page scheme; treat them as external.
  if (raw.startsWith("//")) {
    return { kind: "external", url: raw, external: true };
  }

  const scheme = SCHEME.exec(raw);
  if (scheme) {
    const name = scheme[1].toLowerCase();
    if (name === "http" || name === "https") {
      return { kind: "external", url: raw, external: true };
    }
    if (name === "mailto" && options.kind === "link") {
      return { kind: "external", url: raw, external: true };
    }
    // javascript:, data:, file:, … — never render these.
    return null;
  }

  if (raw.startsWith("#")) {
    return options.kind === "link"
      ? { kind: "external", url: raw, external: false }
      : null;
  }

  // Filesystem lookup ignores queries; keep a fragment for in-page use.
  const hashIndex = raw.indexOf("#");
  const hash = hashIndex === -1 ? "" : raw.slice(hashIndex);
  const withoutHash = hashIndex === -1 ? raw : raw.slice(0, hashIndex);
  const queryIndex = withoutHash.indexOf("?");
  const pathPart = queryIndex === -1 ? withoutHash : withoutHash.slice(0, queryIndex);
  const decoded = decodePath(pathPart);
  if (!decoded) return null;

  const joined = decoded.startsWith("/")
    ? `${options.root}/${decoded.replace(/^\/+/, "")}`
    : `${directoryOf(options.filePath)}/${decoded}`;
  const path = normalizeWorkspacePath(joined);
  if (!path || !isInside(options.root, path)) return null;

  const url = options.rawUrl(options.root, path);
  if (!url) return null;
  return { kind: "workspace", path, url: `${url}${hash}`, external: false };
}
