import { useEffect, useMemo } from "react";
import { useStrings } from "../../hooks/useStrings";
import { store, useAppState } from "../../hooks/useStore";
import { controller } from "../../transport/controller";
import { Dialog } from "../common/Dialog";
import { PanelLoading } from "../common/EmptyState";
import { formatBytes } from "../common/format";

function decodeChunks(chunks: string[]): Uint8Array {
  const parts: Uint8Array[] = [];
  for (const chunk of chunks) {
    try {
      const binary = atob(chunk);
      const bytes = new Uint8Array(binary.length);
      for (let index = 0; index < binary.length; index += 1) {
        bytes[index] = binary.charCodeAt(index);
      }
      parts.push(bytes);
    } catch {
      // A malformed chunk is skipped; the command layer already validated refs.
    }
  }
  const total = parts.reduce((size, part) => size + part.length, 0);
  const output = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    output.set(part, offset);
    offset += part.length;
  }
  return output;
}

function looksBinary(bytes: Uint8Array): boolean {
  const limit = Math.min(bytes.length, 8192);
  for (let index = 0; index < limit; index += 1) {
    if (bytes[index] === 0) return true;
  }
  return false;
}

export function ArtifactViewer({
  artifactRef,
  title,
  onClose,
}: {
  artifactRef: string;
  title?: string;
  onClose: () => void;
}) {
  const strings = useStrings();
  const state = useAppState();
  const artifact = state.ui.artifacts[artifactRef];

  useEffect(() => {
    if (!artifact) controller.readArtifact(artifactRef);
  }, [artifactRef, artifact]);

  const bytes = useMemo(() => decodeChunks(artifact?.chunks ?? []), [artifact?.chunks]);
  const binary = useMemo(() => looksBinary(bytes), [bytes]);
  const text = useMemo(
    () => (binary ? "" : new TextDecoder("utf-8", { fatal: false }).decode(bytes)),
    [binary, bytes],
  );

  const blobUrl = useMemo(() => {
    if (!binary || bytes.length === 0) return null;
    // Copy into a fresh Uint8Array<ArrayBuffer>: a decoded view is typed as
    // Uint8Array<ArrayBufferLike>, which is not assignable to BlobPart.
    const copy = new Uint8Array(bytes.length);
    copy.set(bytes);
    return URL.createObjectURL(new Blob([copy.buffer as ArrayBuffer]));
  }, [binary, bytes]);

  useEffect(
    () => () => {
      if (blobUrl) URL.revokeObjectURL(blobUrl);
    },
    [blobUrl],
  );

  return (
    <Dialog title={title || strings.overlays.artifactTitle} onClose={onClose} wide>
      <div className="artifact-meta">
        <span className="truncate">{artifactRef}</span>
        <span>·</span>
        <span>{formatBytes(artifact?.size)}</span>
        {artifact?.eof ? (
          <span>· {strings.overlays.complete}</span>
        ) : (
          <span>· {strings.overlays.loadingShort}</span>
        )}
      </div>
      {artifact?.error ? (
        <div className="form-error">
          <div>
            {artifact.error.code}: {artifact.error.message}
          </div>
          <button
            className="btn btn-sm"
            style={{ marginTop: 8 }}
            onClick={() =>
              store.dispatch({ type: "intent/readArtifact", ref: artifactRef })
            }
          >
            {strings.overlays.retry}
          </button>
        </div>
      ) : !artifact || (artifact.loading && artifact.chunks.length === 0) ? (
        <PanelLoading label={strings.overlays.loadingArtifact} />
      ) : binary ? (
        <div className="empty-state">
          <strong>{strings.sidebar.binaryFile}</strong>
          <span>{formatBytes(artifact?.size)}</span>
          {blobUrl ? (
            <a className="btn" href={blobUrl} download={`${artifactRef.replace(/[^a-z0-9._-]+/gi, "_")}.bin`}>
              {strings.overlays.download}
            </a>
          ) : null}
        </div>
      ) : (
        <pre className="artifact-pre">{text}</pre>
      )}
      {artifact && artifact.chunks.length > 0 && !artifact.eof ? (
        <div className="small muted">{strings.overlays.artifactTooLarge}</div>
      ) : null}
    </Dialog>
  );
}
