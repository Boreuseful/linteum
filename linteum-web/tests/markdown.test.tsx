import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Markdown } from "../src/ui/chat/Markdown";
import { isFenceClosed } from "../src/ui/chat/normalizeDisplayMath";

describe("isFenceClosed", () => {
  it("accepts a closing fence of the same or greater length", () => {
    expect(isFenceClosed("```mermaid\ngraph TD\nA-->B\n```")).toBe(true);
    expect(isFenceClosed("~~~~mermaid\nx\n~~~~")).toBe(true);
    expect(isFenceClosed("````mermaid\nx\n````")).toBe(true);
  });

  it("rejects a block that is still streaming", () => {
    expect(isFenceClosed("```mermaid\ngraph TD\nA-->B\n")).toBe(false);
    expect(isFenceClosed("````mermaid\nx\n```")).toBe(false);
  });
});

describe("Markdown", () => {
  it("renders inline and display LaTeX with KaTeX", () => {
    const html = renderToStaticMarkup(
      <Markdown
        text={"Energy: $E = mc^2$\n\n$$\n\\int_0^1 x\\,dx = \\frac{1}{2}\n$$"}
      />,
    );

    expect(html).toContain('class="katex"');
    expect(html).toContain("katex-display");
  });

  it("promotes a single-line $$...$$ line to display math", () => {
    const html = renderToStaticMarkup(
      <Markdown text={"Before\n\n$$E = mc^2$$\n\nAfter"} />,
    );

    expect(html).toContain("katex-display");
  });

  it("keeps $$...$$ inline when it sits inside a sentence", () => {
    const html = renderToStaticMarkup(<Markdown text={"Lift ($$L$$) is fine."} />);

    expect(html).toContain('class="katex"');
    expect(html).not.toContain("katex-display");
  });

  it("does not merge two $$...$$ formulas on one line", () => {
    const html = renderToStaticMarkup(<Markdown text={"$$a$$ and $$b$$"} />);

    expect(html).not.toContain("katex-display");
    expect(html.match(/class="katex"/g)).toHaveLength(2);
  });

  it("does not touch $$...$$ inside fenced code", () => {
    const html = renderToStaticMarkup(
      <Markdown text={"```md\n$$E = mc^2$$\n```"} />,
    );

    expect(html).toContain("language-md");
    expect(html).not.toContain("katex-display");
  });

  it("routes a mermaid fence to the diagram renderer", () => {
    const html = renderToStaticMarkup(
      <Markdown text={"```mermaid\ngraph TD\n  A[Start] --> B[Done]\n```"} />,
    );

    expect(html).toContain('class="mermaid-block"');
    expect(html).toContain("graph TD");
    expect(html).not.toContain("language-mermaid");
    expect(html).not.toContain('class="code-block"');
  });

  it("keeps ordinary fenced code as a pre/code block", () => {
    const html = renderToStaticMarkup(
      <Markdown text={"```ts\nconst answer = 42;\n```"} />,
    );

    expect(html).toContain("<pre>");
    expect(html).toContain("language-ts");
    expect(html).not.toContain("mermaid-block");
    expect(html).toContain('class="code-block"');
    expect(html).toContain('aria-label="Copy code"');
  });
});

describe("Markdown resources", () => {
  it("resolves images through the provided resolver", () => {
    const html = renderToStaticMarkup(
      <Markdown
        text={"![diagram](./img/a.png)"}
        resolveImage={() => "raw://a.png"}
      />,
    );

    expect(html).toContain('src="raw://a.png"');
    expect(html).toContain('alt="diagram"');
  });

  it("falls back to alt text when an image is unresolved", () => {
    const html = renderToStaticMarkup(
      <Markdown text={"![diagram](./img/a.png)"} resolveImage={() => null} />,
    );

    expect(html).toContain("diagram");
    expect(html).not.toContain("<img");
  });

  it("opens external links in a new tab", () => {
    const html = renderToStaticMarkup(
      <Markdown text={"[site](https://example.com)"} />,
    );

    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it("renders in-app links without a new-tab target", () => {
    const html = renderToStaticMarkup(
      <Markdown
        text={"[guide](./guide.md)"}
        resolveLink={() => ({ href: "raw://guide.md", external: false })}
      />,
    );

    expect(html).toContain('href="raw://guide.md"');
    expect(html).not.toContain('target="_blank"');
  });

  it("renders unresolved links as plain text", () => {
    const html = renderToStaticMarkup(
      <Markdown text={"[guide](./guide.md)"} resolveLink={() => null} />,
    );

    expect(html).toContain("guide");
    expect(html).not.toContain("<a ");
  });
});
