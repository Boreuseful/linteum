import type {
  WorkspaceEntry,
  WorkspaceFileEntry,
  WorkspaceFileResult,
  WorkspaceFilesResult,
  WorkspaceListResult,
  WorkspaceProvider,
} from "./WorkspaceProvider";

function isEntry(value: unknown): value is WorkspaceEntry {
  if (!value || typeof value !== "object") return false;
  const entry = value as Record<string, unknown>;
  return (
    typeof entry.name === "string" &&
    typeof entry.path === "string" &&
    typeof entry.isDir === "boolean"
  );
}

function isFileEntry(value: unknown): value is WorkspaceFileEntry {
  if (!value || typeof value !== "object") return false;
  const entry = value as Record<string, unknown>;
  return (
    typeof entry.name === "string" &&
    typeof entry.path === "string" &&
    typeof entry.relative === "string"
  );
}

async function readError(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && typeof (body as Record<string, unknown>).error === "string") {
      return (body as Record<string, string>).error;
    }
  } catch {
    // fall through
  }
  return `HTTP ${response.status}`;
}

/**
 * Demo-only adapter: the Vite dev server exposes a read-only, path-contained
 * view of the workspace. Never bundled into a production deployment.
 */
export class DemoWorkspaceProvider implements WorkspaceProvider {
  readonly kind = "demo" as const;

  isAvailable(): boolean {
    return import.meta.env.DEV;
  }

  async list(root: string, path: string): Promise<WorkspaceListResult> {
    try {
      const response = await fetch(
        `/demo-api/workspace/tree?root=${encodeURIComponent(root)}&path=${encodeURIComponent(path)}`,
      );
      if (!response.ok) {
        return { entries: [], error: await readError(response) };
      }
      const body: unknown = await response.json();
      const entries =
        body && typeof body === "object" && Array.isArray((body as Record<string, unknown>).entries)
          ? ((body as Record<string, unknown>).entries as unknown[]).filter(isEntry)
          : [];
      return { entries };
    } catch (error) {
      return {
        entries: [],
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }

  async files(root: string): Promise<WorkspaceFilesResult> {
    try {
      const response = await fetch(
        `/demo-api/workspace/files?root=${encodeURIComponent(root)}`,
      );
      if (!response.ok) {
        return { files: [], error: await readError(response) };
      }
      const body = (await response.json()) as Record<string, unknown>;
      return {
        files: Array.isArray(body.files)
          ? body.files.filter(isFileEntry)
          : [],
        truncated: body.truncated === true,
      };
    } catch (error) {
      return {
        files: [],
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }

  rawUrl(root: string, path: string): string {
    return `/demo-api/workspace/raw?root=${encodeURIComponent(root)}&path=${encodeURIComponent(path)}`;
  }

  async read(root: string, path: string): Promise<WorkspaceFileResult> {
    try {
      const response = await fetch(
        `/demo-api/workspace/file?root=${encodeURIComponent(root)}&path=${encodeURIComponent(path)}`,
      );
      if (!response.ok) {
        return { error: await readError(response) };
      }
      const body = (await response.json()) as Record<string, unknown>;
      return {
        text: typeof body.text === "string" ? body.text : undefined,
        binary: body.binary === true,
        size: typeof body.size === "number" ? body.size : undefined,
        truncated: body.truncated === true,
      };
    } catch (error) {
      return { error: error instanceof Error ? error.message : String(error) };
    }
  }
}
