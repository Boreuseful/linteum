import { Send, Square } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useStrings } from "../../hooks/useStrings";
import { store, useAppState } from "../../hooks/useStore";
import { controller } from "../../transport/controller";
import { SlashMenu, slashCommands, type SlashCommand } from "./SlashMenu";

export function Composer() {
  const strings = useStrings();
  const state = useAppState();
  const text = state.ui.composer;
  const active = state.active;
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [activeIndex, setActiveIndex] = useState(0);

  const filtered = useMemo(() => {
    if (!text.startsWith("/") || text.includes("\n")) return [];
    const query = text.slice(1).toLowerCase();
    return slashCommands(strings).filter(
      (command) =>
        command.command.slice(1).startsWith(query) ||
        command.label.toLowerCase().includes(query),
    );
  }, [text, strings]);

  const slashOpen = filtered.length > 0;

  useEffect(() => {
    setActiveIndex(0);
  }, [text]);

  useEffect(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 220)}px`;
  }, [text]);

  const selectCommand = (command: SlashCommand) => {
    store.dispatch({ type: "ui/composer", text: "" });
    command.run();
  };

  const send = () => {
    const trimmed = text.trim();
    if (!trimmed || !active || state.connection.status !== "ready") return;
    controller.sendMessage(trimmed);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.nativeEvent.isComposing) return;

    if (slashOpen) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActiveIndex((index) => (index + 1) % filtered.length);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setActiveIndex((index) => (index - 1 + filtered.length) % filtered.length);
        return;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        const command = filtered[activeIndex] ?? filtered[0];
        if (command) selectCommand(command);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        store.dispatch({ type: "ui/composer", text: "" });
        return;
      }
    }

    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  };

  const busy = active?.busy === true;
  const connected = state.connection.status === "ready";

  return (
    <div className="composer-shell">
      {slashOpen ? (
        <SlashMenu
          commands={filtered}
          activeIndex={activeIndex}
          onSelect={selectCommand}
          onHover={setActiveIndex}
        />
      ) : null}
      <textarea
        ref={textareaRef}
        className="composer-textarea"
        value={text}
        rows={1}
        spellCheck={false}
        placeholder={busy ? strings.composer.placeholderBusy : strings.composer.placeholder}
        onChange={(event) =>
          store.dispatch({ type: "ui/composer", text: event.target.value })
        }
        onKeyDown={onKeyDown}
        aria-label={strings.composer.messageLabel}
      />
      <div className="composer-bar">
        <span className="composer-hint">
          {busy
            ? strings.composer.busy
            : connected
              ? strings.chat.composerHint
              : strings.chat.waitingForGateway}
        </span>
        <span className="composer-bar-spacer" />
        {busy ? (
          <button className="composer-stop" onClick={() => controller.interrupt()}>
            <Square size={12} fill="currentColor" /> {strings.composer.stop}
          </button>
        ) : null}
        <button
          className="composer-send"
          onClick={send}
          disabled={!text.trim() || !connected}
          title={strings.composer.send}
          aria-label={strings.composer.send}
        >
          <Send size={16} />
        </button>
      </div>
    </div>
  );
}
