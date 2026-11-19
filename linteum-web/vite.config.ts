import { promises as fs } from "node:fs";
import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";
import { parseByteRange } from "./src/workspace/byteRange";

/**
 * Demo-only workspace bridge.
 *
 * A browser cannot read the host filesystem, and Gateway Protocol v1 has no
 * `workspace.*` command yet. The demo therefore exposes a read-only,
 * path-contained view of the session workspace through the Vite dev server.
 * It is intentionally `apply: "serve"` and must never be used in production.
 * The panel consumes it through `src/workspace/WorkspaceProvider.ts`, so a
 * future `workspace.tree` / `workspace.read` gateway adapter can replace it
 * without touching the UI.
 */

const MAX_PREVIEW_BYTES = 256 * 1024;
/** Raw bytes for browser-native viewers (PDF); larger than the text preview. */
const MAX_RAW_BYTES = 32 * 1024 * 1024;
const RAW_CONTENT_TYPES: Record<string, string> = {
  ".pdf": "application/pdf",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".avif": "image/avif",
  ".bmp": "image/bmp",
  ".ico": "image/x-icon",
  ".csv": "text/csv; charset=utf-8",
};
/** Cap for the flat file list shown by the file sidebar picker. */
const MAX_LISTED_FILES = 2000;
/** Heavy generated directories that would swamp the picker. */
const IGNORED_DIRS = new Set([
  "node_modules",
  "venv",
  "__pycache__",
  "target",
  "dist",
  "build",
  "out",
  "coverage",
  "vendor",
]);

function isInside(root: string, target: string): boolean {
  const relative = path.relative(root, target);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

async function resolveRoot(raw: string | null): Promise<string> {
  if (!raw) {
    throw new Error("workspace root is required");
  }
  const root = await fs.realpath(raw);
  const stat = await fs.stat(root);
  if (!stat.isDirectory()) {
    throw new Error("workspace root is not a directory");
  }
  return root;
}

async function resolveTarget(root: string, raw: string | null): Promise<string> {
  const candidate = raw ? path.resolve(root, raw) : root;
  if (!isInside(root, candidate)) {
    throw new Error("path escapes workspace");
  }
  const real = await fs.realpath(candidate);
  if (!isInside(root, real)) {
    throw new Error("path escapes workspace");
  }
  return real;
}

async function collectWorkspaceFiles(root: string): Promise<{
  files: { name: string; path: string; relative: string }[];
  truncated: boolean;
}> {
  const files: { name: string; path: string; relative: string }[] = [];
  const queue: { dir: string; relative: string }[] = [
    { dir: root, relative: "" },
  ];
  let truncated = false;
  while (queue.length > 0 && !truncated) {
    const current = queue.shift();
    if (!current) break;
    let dirents;
    try {
      dirents = await fs.readdir(current.dir, { withFileTypes: true });
    } catch {
      // An unreadable subdirectory is skipped, not fatal for the listing.
      continue;
    }
    for (const dirent of dirents) {
      if (dirent.isSymbolicLink()) continue;
      const relative = current.relative
        ? `${current.relative}/${dirent.name}`
        : dirent.name;
      if (dirent.isDirectory()) {
        if (dirent.name.startsWith(".") || IGNORED_DIRS.has(dirent.name)) continue;
        queue.push({ dir: path.join(current.dir, dirent.name), relative });
      } else if (dirent.isFile()) {
        if (files.length >= MAX_LISTED_FILES) {
          truncated = true;
          break;
        }
        files.push({
          name: dirent.name,
          path: path.join(current.dir, dirent.name),
          relative,
        });
      }
    }
  }
  files.sort((a, b) => a.relative.localeCompare(b.relative));
  return { files, truncated };
}

function workspaceBridge(): Plugin {
  return {
    name: "linteum-demo-workspace-bridge",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use("/demo-api/workspace", async (req, res) => {
        const send = (status: number, body: unknown) => {
          res.statusCode = status;
          res.setHeader("Content-Type", "application/json; charset=utf-8");
          res.end(JSON.stringify(body));
        };

        try {
          const url = new URL(req.url ?? "/", "http://localhost");
          const root = await resolveRoot(url.searchParams.get("root"));
          const target = await resolveTarget(root, url.searchParams.get("path"));

          if (url.pathname === "/files" || url.pathname.endsWith("/files")) {
            const listing = await collectWorkspaceFiles(root);
            send(200, { root, ...listing });
            return;
          }

          if (url.pathname === "/tree" || url.pathname.endsWith("/tree")) {
            const dirents = await fs.readdir(target, { withFileTypes: true });
            const entries = dirents
              .map((dirent) => ({
                name: dirent.name,
                path: path.join(target, dirent.name),
                isDir: dirent.isDirectory(),
              }))
              .sort((a, b) => {
                if (a.isDir !== b.isDir) return a.isDir ? -1 : 1;
                return a.name.localeCompare(b.name);
              });
            send(200, { root, path: target, entries });
            return;
          }

          if (url.pathname === "/raw" || url.pathname.endsWith("/raw")) {
            const stat = await fs.stat(target);
            if (!stat.isFile()) {
              throw new Error("path is not a file");
            }
            if (stat.size > MAX_RAW_BYTES) {
              send(413, { error: "file is too large to preview" });
              return;
            }
            const size = stat.size;
            const contentType =
              RAW_CONTENT_TYPES[path.extname(target).toLowerCase()] ??
              "application/octet-stream";
            res.setHeader("Accept-Ranges", "bytes");
            res.setHeader("X-Content-Type-Options", "nosniff");
            res.setHeader("Cache-Control", "no-store");

            const range = parseByteRange(req.headers.range, size);
            if (range === "invalid") {
              res.statusCode = 416;
              res.setHeader("Content-Range", `bytes */${size}`);
              res.end();
              return;
            }
            const headOnly = req.method === "HEAD";
            if (range) {
              const length = range.end - range.start + 1;
              const handle = await fs.open(target, "r");
              try {
                const buffer = Buffer.alloc(length);
                await handle.read(buffer, 0, length, range.start);
                res.statusCode = 206;
                res.setHeader("Content-Type", contentType);
                res.setHeader("Content-Length", String(length));
                res.setHeader(
                  "Content-Range",
                  `bytes ${range.start}-${range.end}/${size}`,
                );
                res.end(headOnly ? undefined : buffer);
              } finally {
                await handle.close();
              }
              return;
            }

            res.statusCode = 200;
            res.setHeader("Content-Type", contentType);
            res.setHeader("Content-Length", String(size));
            if (headOnly) {
              res.end();
              return;
            }
            const body = await fs.readFile(target);
            res.end(body);
            return;
          }

          if (url.pathname === "/file" || url.pathname.endsWith("/file")) {
            const stat = await fs.stat(target);
            if (!stat.isFile()) {
              throw new Error("path is not a file");
            }
            const length = Math.min(stat.size, MAX_PREVIEW_BYTES);
            const handle = await fs.open(target, "r");
            try {
              const buffer = Buffer.alloc(length);
              await handle.read(buffer, 0, length, 0);
              const binary = buffer.includes(0);
              send(200, {
                root,
                path: target,
                size: stat.size,
                truncated: stat.size > length,
                binary,
                text: binary ? undefined : buffer.toString("utf8"),
              });
            } finally {
              await handle.close();
            }
            return;
          }

          send(404, { error: "unknown demo workspace endpoint" });
        } catch (error) {
          send(400, {
            error: error instanceof Error ? error.message : String(error),
          });
        }
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), workspaceBridge()],
  server: {
    host: "127.0.0.1",
    port: 5173,
  },
});
