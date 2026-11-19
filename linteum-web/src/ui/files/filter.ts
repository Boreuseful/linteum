import type { WorkspaceFileEntry } from "../../workspace";

/** Case-insensitive substring match over workspace-relative paths. */
export function filterFileEntries(
  files: readonly WorkspaceFileEntry[],
  query: string,
): WorkspaceFileEntry[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [...files];
  return files.filter((file) => file.relative.toLowerCase().includes(needle));
}
