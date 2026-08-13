import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { initFlagPolyfill } from "./flagPolyfill";
import "./i18n";

initFlagPolyfill();

const root = document.getElementById("root");

if (root === null) {
  throw new Error("Missing #root element");
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
