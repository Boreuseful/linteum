/**
 * PDF preview rendered by the browser's native viewer. Zooming and paging are
 * left to the embedded viewer's own controls.
 */
export function PdfPreview({ src, name }: { src: string; name: string }) {
  return (
    <div className="file-pdf-stage">
      <iframe className="file-pdf-frame" src={src} title={name} />
    </div>
  );
}
