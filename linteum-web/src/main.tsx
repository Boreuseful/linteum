import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app";

import "./theme/tokens.css";
import "./theme/themes/dark.css";
import "./theme/themes/light.css";
import "./theme/themes/mono.css";
import "./styles/base.css";
import "./styles/layout.css";
import "./styles/chat.css";
import "./styles/panels.css";
import "./styles/settings.css";

const container = document.getElementById("root");
if (!container) {
  throw new Error("root element is missing");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
