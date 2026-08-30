import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
// Style order matters: the app adapter layer first, then the type bridge
// (font APPLICATION only — the one documented purity-scan exception).
import "./styles/app.css";
import "./styles/type-bridge.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
