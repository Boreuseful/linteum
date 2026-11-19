import { ArrowDown, MessageSquare } from "lucide-react";
import { useLayoutEffect, useRef, useState } from "react";
import { useStrings } from "../../hooks/useStrings";
import { useAppState } from "../../hooks/useStore";
import { controller } from "../../transport/controller";
import { EmptyState } from "../common/EmptyState";
import { evaluateScroll } from "./scroll";
import { TranscriptItemView } from "./TranscriptItemView";

export function ChatView() {
  const strings = useStrings();
  const state = useAppState();
  const active = state.active;
  const scrollRef = useRef<HTMLDivElement>(null);
  const lastScrollTop = useRef(0);
  // Synchronous copy of `atBottom`: a scroll event must win over a transcript
  // delta committed in the same frame, so the effect reads the ref, not state.
  const stickRef = useRef(true);
  const [atBottom, setAtBottom] = useState(true);

  const scrollToBottom = (behavior: ScrollBehavior = "auto") => {
    const element = scrollRef.current;
    if (!element) return;
    element.scrollTo({ top: element.scrollHeight, behavior });
    // Remember where our own scroll landed; its event must not look like an
    // upward move if content grew before the event was delivered.
    lastScrollTop.current = element.scrollTop;
  };

  // Layout effect: scroll after React has committed the new transcript but
  // before the frame paints, so scrollHeight is final when we pin.
  useLayoutEffect(() => {
    if (stickRef.current) scrollToBottom();
  }, [active?.transcript]);

  useLayoutEffect(() => {
    stickRef.current = true;
    setAtBottom(true);
    const element = scrollRef.current;
    if (!element) return;
    element.scrollTop = element.scrollHeight;
    lastScrollTop.current = element.scrollTop;
  }, [active?.id]);

  if (!active) return null;

  const onScroll = () => {
    const element = scrollRef.current;
    if (!element) return;
    const top = element.scrollTop;
    const decision = evaluateScroll({
      top,
      height: element.scrollHeight,
      viewport: element.clientHeight,
      previousTop: lastScrollTop.current,
    });
    lastScrollTop.current = top;
    if (decision === "bottom") {
      stickRef.current = true;
      setAtBottom(true);
    } else if (decision === "detach") {
      stickRef.current = false;
      setAtBottom(false);
    }
    // "ignore": a programmatic re-pin that got overtaken by content growth.
  };

  return (
    <div className="chat-view">
      {state.connection.status !== "ready" ? (
        <div className="reconnect-banner" role="status">
          <span className={`status-dot ${state.connection.status}`} />
          {state.connection.status === "reconnecting"
            ? strings.chat.reconnecting
            : `${strings.chat.connection}: ${state.connection.status}`}
          {state.connection.error ? ` — ${state.connection.error.message}` : ""}
        </div>
      ) : null}
      {active.phase === "paused" ? (
        <div className="reconnect-banner" role="status">
          <span className="status-dot error" />
          {strings.chat.agentPaused}
          <button className="btn btn-sm" onClick={() => controller.resume()}>
            {strings.chat.resume}
          </button>
        </div>
      ) : null}
      <div className="transcript-scroll" ref={scrollRef} onScroll={onScroll}>
        <div className="transcript-inner">
          {active.hydrating ? (
            <div className="transcript-loading" role="status" aria-live="polite">
              <span className="spinner spinner-lg" />
              <strong>{strings.chat.loadingSession}</strong>
              <span className="transcript-loading-hint">
                {strings.chat.loadingSessionHint}
              </span>
            </div>
          ) : active.transcript.length === 0 ? (
            <EmptyState icon={<MessageSquare size={26} />} title={strings.chat.emptySession} />
          ) : (
            active.transcript.map((item) => (
              <TranscriptItemView key={item.id} item={item} />
            ))
          )}
          {!atBottom ? (
            <div className="jump-to-bottom">
              <button
                className="btn btn-sm"
                onClick={() => {
                  stickRef.current = true;
                  setAtBottom(true);
                  scrollToBottom("smooth");
                }}
              >
                <ArrowDown size={14} /> {strings.chat.jumpToBottom}
              </button>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
