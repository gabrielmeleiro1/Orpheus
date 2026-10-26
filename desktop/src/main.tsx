import React from "react";
import ReactDOM from "react-dom/client";

import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "./styles.css";
import App from "./App";

const container = document.getElementById("root");
if (!container) {
  throw new Error("index.html is missing the #root mount point");
}

ReactDOM.createRoot(container).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
