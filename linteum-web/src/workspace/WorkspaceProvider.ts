export interface WorkspaceEntry {
  name: string;
  path: string;
  isDir: boolean;
}

export interface WorkspaceListResult {
  entries: WorkspaceEntry[];
  error?: string;
}

export interface WorkspaceFileResult {
  text?: string;
  binary?: boolean;
  size?: number;
  truncated?: boolean;
  error?: string;
}

/** One file in the flat workspace list used by the file sidebar picker. */
export interface WorkspaceFileEntry {
  name: string;
  path: string;
  /** Path relative to the workspace root, for display and filtering. */
  relative: string;
}

export interface WorkspaceFilesResult {
  files: WorkspaceFileEntry[];
  /** The walk hit its cap; the count is a lower bound. */
  truncated?: boolean;
  error?: string;
}

/**
 * Read-only workspace port.
 *
 * Today the demo adapter talks to the Vite dev bridge; a future
 * `workspace.tree` / `workspace.read` gateway command drops in here without
 * changing the panel.
 */
export interface WorkspaceProvider {
  readonly kind: "demo" | "gateway";
  isAvailable(): boolean;
  list(root: string, path: string): Promise<WorkspaceListResult>;
  read(root: string, path: string): Promise<WorkspaceFileResult>;
  /** Flat recursive file list for the file sidebar picker. */
  files(root: string): Promise<WorkspaceFilesResult>;
  /**
   * Same-origin URL that streams the raw file bytes, used for formats the
   * browser renders itself (PDF). `null` when the provider cannot serve bytes.
   */
  rawUrl(root: string, path: string): string | null;
}
