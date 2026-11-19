import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it } from "vitest";
import { store } from "../src/state/instance";
import { en } from "../src/strings";
import { ToolInvocationView } from "../src/ui/common/ToolInvocationView";

beforeEach(() => {
  store.dispatch({ type: "ui/locale", locale: "en" });
});

describe("ToolInvocationView", () => {
  it("renders a bash command as a labelled code field with a raw toggle", () => {
    const html = renderToStaticMarkup(
      <ToolInvocationView name="bash" args={{ command: "git status" }} />,
    );
    expect(html).toContain(en.chat.toolCommand);
    expect(html).toContain("git status");
    expect(html).toContain(en.chat.showRaw);
  });

  it("renders a file path as a code chip", () => {
    const html = renderToStaticMarkup(
      <ToolInvocationView
        name="write_file"
        args={{ path: "src/app.ts", text: "export {};" }}
      />,
    );
    expect(html).toContain("src/app.ts");
    expect(html).toContain('class="tool-path"');
    expect(html).toContain("export {};");
  });

  it("falls back to raw JSON for unknown tools", () => {
    const html = renderToStaticMarkup(
      <ToolInvocationView name="web_search" args={{ q: "hello" }} />,
    );
    expect(html).toContain("hello");
    expect(html).not.toContain(en.chat.showRaw);
  });
});
