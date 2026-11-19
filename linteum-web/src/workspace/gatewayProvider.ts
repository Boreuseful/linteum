import type {
  WorkspaceFileResult,
  WorkspaceFilesResult,
  WorkspaceListResult,
  WorkspaceProvider,
} from "./WorkspaceProvider";

/**
 * Future adapter for Gateway Protocol `workspace.tree` / `workspace.read`.
 * Not implemented in protocol v1; the panel shows a capability message.
 */
export class GatewayWorkspaceProvider implements WorkspaceProvider {
  readonly kind = "gateway" as const;

  isAvailable(): boolean {
    return false;
  }

  async list(_root: string, _path: string): Promise<WorkspaceListResult> {
    return {
      entries: [],
      error: "workspace.* is not implemented in Gateway Protocol v1",
    };
  }

  async read(_root: string, _path: string): Promise<WorkspaceFileResult> {
    return {
      error: "workspace.* is not implemented in Gateway Protocol v1",
    };
  }

  async files(_root: string): Promise<WorkspaceFilesResult> {
    return {
      files: [],
      error: "workspace.* is not implemented in Gateway Protocol v1",
    };
  }

  rawUrl(_root: string, _path: string): string | null {
    return null;
  }
}
