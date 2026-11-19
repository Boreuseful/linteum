import { DemoWorkspaceProvider } from "./demoBridge";
import { GatewayWorkspaceProvider } from "./gatewayProvider";
import type { WorkspaceProvider } from "./WorkspaceProvider";

export const workspaceProvider: WorkspaceProvider = import.meta.env.DEV
  ? new DemoWorkspaceProvider()
  : new GatewayWorkspaceProvider();

export type {
  WorkspaceEntry,
  WorkspaceFileEntry,
  WorkspaceFileResult,
  WorkspaceFilesResult,
  WorkspaceListResult,
} from "./WorkspaceProvider";
export type { WorkspaceProvider };
