import { useMemo } from "react";
import { store } from "../../hooks/useStore";
import { workspaceProvider } from "../../workspace";
import { Markdown } from "../chat/Markdown";
import { resolveMarkdownTarget } from "./markdownTargets";

function basename(path: string): string {
  const index = path.lastIndexOf("/");
  return index === -1 ? path : path.slice(index + 1);
}

function rawUrl(root: string, path: string): string | null {
  return workspaceProvider.rawUrl(root, path);
}

/**
 * Workspace Markdown preview: the chat renderer (KaTeX, Mermaid, highlighted
 * code) plus workspace-aware images and links. Relative links open in the file
 * pane; external links open in a new tab.
 */
export function MarkdownFilePreview({
  text,
  root,
  path,
}: {
  text: string;
  root: string;
  path: string;
}) {
  const resources = useMemo(
    () => ({
      resolveImage: (src: string) => {
        const target = resolveMarkdownTarget(src, {
          root,
          filePath: path,
          kind: "image",
          rawUrl,
        });
        return target?.url ?? null;
      },
      resolveLink: (href: string) => {
        const target = resolveMarkdownTarget(href, {
          root,
          filePath: path,
          kind: "link",
          rawUrl,
        });
        if (!target) return null;
        if (target.kind === "external") {
          return { href: target.url, external: target.external };
        }
        return {
          href: target.url,
          external: false,
          onClick: () => {
            store.dispatch({
              type: "file/open",
              root,
              path: target.path,
              name: basename(target.path),
            });
            return true;
          },
        };
      },
    }),
    [root, path],
  );

  return (
    <div className="file-markdown-preview">
      <Markdown text={text} {...resources} />
    </div>
  );
}
