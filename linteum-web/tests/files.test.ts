import { describe, expect, it } from "vitest";
import { filterFileEntries } from "../src/ui/files/filter";
import type { WorkspaceFileEntry } from "../src/workspace";

const files: WorkspaceFileEntry[] = [
  { name: "a.py", path: "/w/a.py", relative: "a.py" },
  { name: "main.rs", path: "/w/src/main.rs", relative: "src/main.rs" },
  { name: "README.md", path: "/w/README.md", relative: "README.md" },
];

describe("filterFileEntries", () => {
  it("returns every file for an empty or whitespace query", () => {
    expect(filterFileEntries(files, "")).toEqual(files);
    expect(filterFileEntries(files, "   ")).toEqual(files);
  });

  it("matches relative paths case-insensitively", () => {
    expect(filterFileEntries(files, "SRC/").map((file) => file.relative)).toEqual([
      "src/main.rs",
    ]);
    expect(filterFileEntries(files, "readme").map((file) => file.relative)).toEqual([
      "README.md",
    ]);
  });

  it("returns no entries when nothing matches", () => {
    expect(filterFileEntries(files, "zzz")).toEqual([]);
  });
});
