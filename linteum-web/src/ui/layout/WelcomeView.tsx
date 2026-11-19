import { MessageSquare, Plus } from "lucide-react";
import { useStrings } from "../../hooks/useStrings";
import { store } from "../../hooks/useStore";

export function WelcomeView() {
  const strings = useStrings();
  return (
    <div className="welcome">
      <div className="welcome-icon">
        <MessageSquare size={26} />
      </div>
      <h1>{strings.chat.welcomeTitle}</h1>
      <p>{strings.chat.welcomeBody}</p>
      <button
        className="btn btn-primary"
        onClick={() => store.dispatch({ type: "ui/openOverlay", overlay: { kind: "new-session" } })}
      >
        <Plus size={15} /> {strings.chat.newSession}
      </button>
    </div>
  );
}
