export function defaultOpenPanels(): Record<string, boolean> {
  return {
    subagents: true,
    background: false,
    context: false,
    info: false,
    workspace: false,
    plugins: false,
  };
}

export function toggleOpenPanel(
  openPanels: Record<string, boolean>,
  panelId: string,
): Record<string, boolean> {
  return { ...openPanels, [panelId]: !openPanels[panelId] };
}
