import {
  createContext,
  memo,
  useContext,
  useMemo,
  type ComponentProps,
  type MouseEvent,
} from "react";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import ReactMarkdown, { type Components, type ExtraProps } from "react-markdown";

import "katex/dist/katex.min.css";
import { CodeBlock } from "./CodeBlock";
import { MermaidDiagram } from "./Mermaid";
import { isFenceClosed, normalizeDisplayMath } from "./normalizeDisplayMath";

/** Minimal structural view of the hast nodes react-markdown hands to components. */
interface HastNode {
  type: string;
  tagName?: string;
  value?: string;
  properties?: { className?: unknown };
  children?: HastNode[];
  position?: {
    start: { offset?: number | null };
    end: { offset?: number | null };
  };
}

function textContent(node: HastNode): string {
  if (node.type === "text") return node.value ?? "";
  return (node.children ?? []).map(textContent).join("");
}

/** Returns the source when `node` is `<pre><code class="language-mermaid">`. */
function mermaidSource(node: HastNode | undefined): string | null {
  const code = node?.children?.find(
    (child) => child.type === "element" && child.tagName === "code",
  );
  if (!code) return null;
  const className = code.properties?.className;
  const classes =
    typeof className === "string"
      ? className.split(/\s+/)
      : Array.isArray(className)
        ? className.map(String)
        : [];
  if (!classes.some((name) => name.toLowerCase() === "language-mermaid")) return null;
  return textContent(code).replace(/\n$/, "");
}

/**
 * True once the block's closing fence has been emitted. Until then the source
 * is still streaming and Mermaid would parse a truncated diagram.
 */
function blockClosed(node: HastNode | undefined, source: string): boolean {
  const start = node?.position?.start.offset;
  const end = node?.position?.end.offset;
  if (start == null || end == null) return true;
  return isFenceClosed(source.slice(start, end));
}

/** An `<a>` target after document-specific resolution. */
export interface MarkdownLinkTarget {
  href: string;
  /** Open in a new tab. Defaults to true. */
  external?: boolean;
  /** Return true when the click was handled in-app (navigation is prevented). */
  onClick?: () => boolean;
}

/** Optional overrides that let a preview resolve workspace-relative resources. */
export interface MarkdownResourceOptions {
  /** Resolve an `<img src>`; return `null` to show the alt text instead. */
  resolveImage?: (src: string) => string | null;
  /** Resolve an `<a href>`; return `null` to render the label as plain text. */
  resolveLink?: (href: string) => MarkdownLinkTarget | null;
}

interface MarkdownContextValue extends MarkdownResourceOptions {
  source: string;
  streaming: boolean;
}

/**
 * The provider value changes with every streamed delta, while the component
 * types stay stable. That lets each diagram keep its rendered SVG instead of
 * being unmounted when the transcript re-renders.
 */
const MarkdownContext = createContext<MarkdownContextValue>({
  source: "",
  streaming: false,
});

function MarkdownLink({
  node: _node,
  href,
  children,
  ...props
}: ComponentProps<"a"> & ExtraProps) {
  const { resolveLink } = useContext(MarkdownContext);
  const target =
    href === undefined ? null : resolveLink ? resolveLink(href) : { href };
  if (target === null) {
    return <span className="markdown-link-text">{children}</span>;
  }
  const onClick = (event: MouseEvent<HTMLAnchorElement>) => {
    if (target.onClick?.()) event.preventDefault();
  };
  const external = target.external !== false;
  return (
    <a
      {...props}
      href={target.href}
      {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})}
      onClick={onClick}
    >
      {children}
    </a>
  );
}

function MarkdownImage({
  node: _node,
  src,
  alt,
  ...props
}: ComponentProps<"img"> & ExtraProps) {
  const { resolveImage } = useContext(MarkdownContext);
  const resolved =
    typeof src === "string" ? (resolveImage ? resolveImage(src) : src) : null;
  if (!resolved) {
    return alt ? <span className="markdown-image-alt">{alt}</span> : null;
  }
  return <img {...props} src={resolved} alt={alt ?? ""} loading="lazy" />;
}

function MarkdownPre({ node, children, ...props }: ComponentProps<"pre"> & ExtraProps) {
  const { source, streaming } = useContext(MarkdownContext);
  const diagram = mermaidSource(node);
  if (diagram !== null) {
    // While the message streams, render as soon as this diagram's fence closes;
    // a diagram left unclosed until the message ends is rendered as a fallback.
    return (
      <MermaidDiagram code={diagram} ready={!streaming || blockClosed(node, source)} />
    );
  }
  return (
    <CodeBlock code={node ? textContent(node).replace(/\n$/, "") : ""}>
      <pre {...props}>{children}</pre>
    </CodeBlock>
  );
}

const COMPONENTS: Components = {
  a: MarkdownLink,
  img: MarkdownImage,
  pre: MarkdownPre,
};

/**
 * Markdown is rendered without raw HTML (`rehype-raw` is deliberately absent),
 * so model output cannot inject elements. Links open outside the app.
 *
 * Math is parsed from `$...$` / `$$...$$` and rendered by KaTeX. Mermaid fences
 * are rendered client-side once their closing fence arrives; before that the
 * source is shown, because partially streamed diagrams are usually invalid.
 *
 * The component is memoised (ChatView re-renders on scroll) and the renderer
 * map is module-level, so a re-render never remounts a diagram.
 */
export const Markdown = memo(function Markdown({
  text,
  streaming = false,
  resolveImage,
  resolveLink,
}: {
  text: string;
  streaming?: boolean;
} & MarkdownResourceOptions) {
  const source = useMemo(() => normalizeDisplayMath(text), [text]);
  const context = useMemo<MarkdownContextValue>(
    () => ({ source, streaming, resolveImage, resolveLink }),
    [source, streaming, resolveImage, resolveLink],
  );

  return (
    <MarkdownContext.Provider value={context}>
      <div className="markdown">
        <ReactMarkdown
          remarkPlugins={[remarkGfm, remarkMath]}
          rehypePlugins={[[rehypeKatex, { throwOnError: false }], rehypeHighlight]}
          components={COMPONENTS}
        >
          {source}
        </ReactMarkdown>
      </div>
    </MarkdownContext.Provider>
  );
});
