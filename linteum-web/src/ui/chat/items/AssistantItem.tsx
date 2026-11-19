import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { useStrings } from "../../../hooks/useStrings";
import type { AssistantItem as AssistantItemModel } from "../../../state/types";
import { copyText } from "../../common/format";
import { Markdown } from "../Markdown";
import { Reasoning } from "../Reasoning";
import { StreamingCursor } from "../StreamingCursor";

export function AssistantItemView({ item }: { item: AssistantItemModel }) {
  const strings = useStrings();
  const [copied, setCopied] = useState(false);
  const model =
    item.provider && item.model ? `${item.provider}/${item.model}` : item.model;

  const onCopy = async () => {
    if (await copyText(item.text)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }
  };

  return (
    <div className="msg msg-assistant">
      <Reasoning text={item.reasoning} />
      <div className="msg-head">
        <span className="msg-role">{strings.chat.assistant}</span>
        {model ? <span className="msg-meta">{model}</span> : null}
        {item.interrupted ? (
          <span className="badge badge-warning">{strings.chat.interrupted}</span>
        ) : null}
        <div className="msg-actions">
          <button
            className="icon-btn"
            onClick={() => void onCopy()}
            title={strings.chat.copy}
            aria-label={strings.chat.copy}
          >
            {copied ? <Check size={14} /> : <Copy size={14} />}
          </button>
        </div>
      </div>
      <div className="assistant-body">
        {item.text ? <Markdown text={item.text} streaming={item.streaming} /> : null}
        {item.streaming ? <StreamingCursor /> : null}
      </div>
    </div>
  );
}
