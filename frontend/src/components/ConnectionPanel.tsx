import { useState, type FormEvent } from "react";
import type { AuthenticationInput } from "../api-client";

type Props = { busy: boolean; onClose: () => void; onSubmit: (input: AuthenticationInput) => Promise<void> };

export function ConnectionPanel({ busy, onClose, onSubmit }: Props) {
  const [intent, setIntent] = useState<AuthenticationInput["intent"]>("register");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  async function handleSubmit(event: FormEvent) { event.preventDefault(); await onSubmit({ intent, name, email, password }); }
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="connection-dialog" role="dialog" aria-modal="true" aria-labelledby="connection-title" onMouseDown={(event) => event.stopPropagation()}>
        <div className="dialog-heading"><div>
          <span className="eyebrow">Live API</span><h2 id="connection-title">Connect to the production workflow</h2>
          <p>Tokens stay in memory and refresh automatically. Signing out revokes the server session.</p>
        </div><button className="icon-button" onClick={onClose} aria-label="Close">×</button></div>
        <div className="auth-tabs" aria-label="Authentication mode">
          <button className={intent === "register" ? "active" : ""} onClick={() => setIntent("register")}>Create account</button>
          <button className={intent === "login" ? "active" : ""} onClick={() => setIntent("login")}>Sign in</button>
        </div>
        <form onSubmit={handleSubmit}>
          {intent === "register" ? <label>Name<input required minLength={2} value={name} onChange={(event) => setName(event.target.value)} /></label> : null}
          <label>Email<input required type="email" value={email} onChange={(event) => setEmail(event.target.value)} /></label>
          <label>Password<input required type="password" minLength={10} value={password} onChange={(event) => setPassword(event.target.value)} placeholder="10+ characters with letters and numbers" /></label>
          <div className="dialog-actions"><button type="button" className="secondary" onClick={onClose}>Cancel</button>
            <button className="primary" disabled={busy}>{busy ? "Connecting…" : intent === "register" ? "Create and connect" : "Sign in"}</button></div>
        </form>
      </section>
    </div>
  );
}
