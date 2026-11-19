import { ShieldAlert } from "lucide-react";
import { useEffect, useState } from "react";
import { useStrings } from "../../hooks/useStrings";
import { useAppState } from "../../hooks/useStore";
import type { ToolItem } from "../../state/types";
import { controller } from "../../transport/controller";
import { ToolInvocationView } from "../common/ToolInvocationView";
import {
  parseArgsPreview,
  presentToolArgs,
  rawArgsText,
} from "../common/toolPresentation";

export function ApprovalDialog() {
  const strings = useStrings();
  const state = useAppState();
  const approval = state.active?.approvals[0];
  const queue = state.active?.approvals.length ?? 0;
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    setSubmitting(false);
  }, [approval?.callId]);

  const decide = (decision: "grant" | "deny") => {
    if (!approval || submitting) return;
    setSubmitting(true);
    controller.decideApproval(approval.callId, decision);
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!approval || submitting) return;
      if (event.key === "y") {
        event.preventDefault();
        decide("grant");
      } else if (event.key === "n") {
        event.preventDefault();
        decide("deny");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [approval, submitting]);

  if (!approval) return null;

  // The approval frame only carries a redacted, truncated preview. When the
  // transcript already holds a built-in workspace tool call, prefer its full
  // arguments so the dialog can show the complete file content/command (and the
  // raw JSON). Unknown/plugin tools stay on the gateway-redacted preview.
  const tool = state.active?.transcript.find(
    (entry): entry is ToolItem =>
      entry.kind === "tool" && entry.callId === approval.callId,
  );
  const fullArgs = tool?.args;
  const fullPresentation = tool
    ? presentToolArgs(tool.name, tool.args)
    : null;
  const useFullArgs =
    fullArgs !== undefined && fullPresentation?.structured === true;
  const preview = parseArgsPreview(approval.argsPreview);
  // A truncated preview is still a JSON prefix: feed it through the partial
  // extractor so a long bash command/write content stays readable.
  const previewArgs =
    !preview.parsed && preview.raw.trimStart().startsWith("{")
      ? { truncated: true, preview: preview.raw }
      : preview.args;
  const args = useFullArgs ? fullArgs : previewArgs;
  const rawText = useFullArgs ? rawArgsText(fullArgs) : preview.raw;
  const toolName =
    tool?.name || approval.name || approval.tool || strings.chat.toolFallback;

  return (
    <div className="overlay-root">
      <div className="dialog" role="alertdialog" aria-modal="true" aria-label={strings.chat.approvalRequired}>
        <div className="dialog-head">
          <h2>{strings.chat.approvalRequired}</h2>
          {queue > 1 ? (
            <span className="approval-queue">
              {queue} {strings.chat.pending}
            </span>
          ) : null}
        </div>
        <div className="dialog-body">
          <div className="approval-summary">
            <div className="approval-tool">
              <ShieldAlert size={16} />
              {toolName}
            </div>
            <div className="row">
              {approval.sideEffect ? (
                <span className="badge badge-warning">{approval.sideEffect}</span>
              ) : null}
              {approval.kind ? <span className="badge">{approval.kind}</span> : null}
              {approval.expiresAt ? (
                <span className="small muted">
                  {strings.chat.expires}{" "}
                  {new Date(approval.expiresAt * 1000).toLocaleTimeString()}
                </span>
              ) : null}
            </div>
            {args !== undefined ? (
              <ToolInvocationView name={toolName} args={args} rawText={rawText} />
            ) : preview.raw ? (
              <pre className="approval-args">{preview.raw}</pre>
            ) : null}
            <div className="small muted">{strings.chat.approvalHint}</div>
          </div>
        </div>
        <div className="dialog-foot">
          <button className="btn" onClick={() => decide("deny")} disabled={submitting}>
            {strings.chat.deny} <span className="kbd">n</span>
          </button>
          <button className="btn btn-primary" onClick={() => decide("grant")} disabled={submitting}>
            {strings.chat.approve} <span className="kbd">y</span>
          </button>
        </div>
      </div>
    </div>
  );
}
