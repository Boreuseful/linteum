import {
  Bot,
  FileText,
  FolderTree,
  Info,
  ListTodo,
  Puzzle,
  type LucideIcon,
} from "lucide-react";
import type { ComponentType } from "react";
import type { Strings } from "../../strings";
import { workspaceProvider } from "../../workspace";
import { BackgroundPanel } from "./panels/BackgroundPanel";
import { ContextPanel } from "./panels/ContextPanel";
import { InfoPanel } from "./panels/InfoPanel";
import { PluginsPanel } from "./panels/PluginsPanel";
import { SubagentsPanel } from "./panels/SubagentsPanel";
import { WorkspacePanel } from "./panels/WorkspacePanel";
import type { PanelProps } from "./types";

export interface SidebarPanel {
  id: string;
  /** Resolved at render time so panel titles follow the active locale. */
  title: (strings: Strings) => string;
  icon: LucideIcon;
  order: number;
  defaultOpen?: boolean;
  isAvailable?: () => boolean;
  Component: ComponentType<PanelProps>;
}

/**
 * The ONLY place that decides which panels exist in the right sidebar.
 * Add/remove/reorder here; PanelHost and the rest of the app stay untouched.
 */
export const PANELS: SidebarPanel[] = [
  {
    id: "subagents",
    title: (s) => s.sidebar.subagents,
    icon: Bot,
    order: 10,
    defaultOpen: true,
    Component: SubagentsPanel,
  },
  {
    id: "background",
    title: (s) => s.sidebar.background,
    icon: ListTodo,
    order: 20,
    defaultOpen: false,
    Component: BackgroundPanel,
  },
  {
    id: "context",
    title: (s) => s.sidebar.context,
    icon: FileText,
    order: 30,
    Component: ContextPanel,
  },
  {
    id: "info",
    title: (s) => s.sidebar.info,
    icon: Info,
    order: 40,
    Component: InfoPanel,
  },
  {
    id: "workspace",
    title: (s) => s.sidebar.workspace,
    icon: FolderTree,
    order: 50,
    isAvailable: () => workspaceProvider.isAvailable(),
    Component: WorkspacePanel,
  },
  {
    id: "plugins",
    title: (s) => s.sidebar.plugins,
    icon: Puzzle,
    order: 60,
    Component: PluginsPanel,
  },
];

export function availablePanels(): SidebarPanel[] {
  return PANELS.filter((panel) => !panel.isAvailable || panel.isAvailable()).sort(
    (a, b) => a.order - b.order,
  );
}
