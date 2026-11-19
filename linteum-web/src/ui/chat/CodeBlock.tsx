import { Check, Copy } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useStrings } from "../../hooks/useStrings";
import { copyText } from "../common/format";

/** Wraps a rendered `<pre>` with the same hover copy action as Mermaid blocks. */
export function CodeBlock({ code, children }: { code: string; children: ReactNode }) {
  const strings = useStrings();
  const [copied, setCopied] = useState(false);

  const onCopy = async () => {
    if (await copyText(code)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }
  };

  return (
    <div className="code-block">
      <div className="code-actions">
        <button
          className="icon-btn"
          onClick={() => void onCopy()}
          title={strings.chat.copyCode}
          aria-label={strings.chat.copyCode}
        >
          {copied ? <Check size={14} /> : <Copy size={14} />}
        </button>
      </div>
      {children}
    </div>
  );
}
