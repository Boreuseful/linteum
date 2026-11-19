import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";
import { useStrings } from "../../hooks/useStrings";

export function Reasoning({
  text,
  defaultOpen = false,
}: {
  text: string;
  defaultOpen?: boolean;
}) {
  const strings = useStrings();
  const [open, setOpen] = useState(defaultOpen);
  if (!text) return null;
  return (
    <div className="reasoning">
      <button
        className="reasoning-toggle"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        {open ? strings.chat.hideReasoning : strings.chat.showReasoning}
      </button>
      {open ? <div className="reasoning-body">{text}</div> : null}
    </div>
  );
}
