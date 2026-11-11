import React from "react";
import { createRoot } from "react-dom/client";
import { consumePasswordResetToken } from "./account-utils";
import { App } from "./App";
import "./styles.css";

const passwordResetToken = consumePasswordResetToken(window.location, window.history);

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App initialResetToken={passwordResetToken} />
  </React.StrictMode>,
);
