import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it } from "vitest";
import { store } from "../src/state/instance";
import type { ToolItem } from "../src/state/types";
import { en } from "../src/strings";
import { ToolItemView } from "../src/ui/chat/items/ToolItem";

function item(overrides: Partial<ToolItem>): ToolItem {
  return {
    kind: "tool",
    id: "tool-call-1",
    seq: 1,
    ordinal: 0,
    createdAt: 0,
    callId: "call-1",
    name: "bash",
    args: {},
    status: "failed",
    ...overrides,
  };
}

beforeEach(() => {
  store.dispatch({ type: "ui/locale", locale: "en" });
});

describe("ToolItemView", () => {
  it("shows the bash command and splits stdout/stderr from the exit code", () => {
    const html = renderToStaticMarkup(
      <ToolItemView
        item={item({
          name: "bash",
          args: { command: "git status" },
          output: { returncode: 2, stdout: "fatal", stderr: "oops", cwd: "." },
        })}
      />,
    );
    expect(html).toContain("git status");
    expect(html).toContain(en.chat.toolCommand);
    expect(html).toContain(en.chat.toolStdout);
    expect(html).toContain(en.chat.toolStderr);
    expect(html).toContain(`${en.chat.toolExitCode}: 2`);
  });

  it("shows a written file's path and content", () => {
    const html = renderToStaticMarkup(
      <ToolItemView
        item={item({
          name: "write_file",
          args: { path: "src/app.ts", text: "export const x = 1;" },
        })}
      />,
    );
    expect(html).toContain("src/app.ts");
    expect(html).toContain(en.chat.toolContent);
    expect(html).toContain("export const x = 1;");
  });

  it("shows a read file's path and contents", () => {
    const html = renderToStaticMarkup(
      <ToolItemView
        item={item({
          name: "read_file",
          args: { path: "README.md" },
          output: "line one\nline two",
        })}
      />,
    );
    expect(html).toContain("README.md");
    expect(html).toContain(en.chat.toolFileContent);
    expect(html).toContain("line two");
  });

  it("shows the workspace root for a list_dir call without arguments", () => {
    const html = renderToStaticMarkup(
      <ToolItemView item={item({ name: "list_dir", args: {} })} />,
    );
    expect(html).toContain(en.chat.toolDirectory);
    expect(html).toContain('<code class="tool-path">.</code>');
  });

  it("keeps unknown tools on the raw JSON rendering", () => {
    const html = renderToStaticMarkup(
      <ToolItemView item={item({ name: "web_search", args: { q: "hello" } })} />,
    );
    expect(html).toContain("hello");
    expect(html).not.toContain(en.chat.showRaw);
  });
});
