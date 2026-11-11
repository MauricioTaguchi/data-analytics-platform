import { useEffect, useRef, useState, type FormEvent } from "react";
import { confirmPasswordReset, requestPasswordReset, type AuthenticationInput } from "../api-client";
import { accountErrorMessage, passwordValidationMessage, type AccountMode } from "../account-utils";
import { useDialogFocus } from "../hooks/useDialogFocus";
import "./AccountPanel.css";

type Props = {
  busy: boolean;
  initialMode?: AccountMode;
  initialEmail?: string;
  resetToken?: string;
  onClose: () => void;
  onSubmit: (input: AuthenticationInput) => Promise<void>;
  onPasswordReset: () => Promise<void>;
};

const COPY: Record<AccountMode, { title: string; description: string; submit: string }> = {
  login: { title: "Sign in to DataFlow", description: "Continue working with your saved projects and datasets.", submit: "Sign in" },
  register: { title: "Create your account", description: "Save your datasets, revisit your work, and keep a history of your changes.", submit: "Create account" },
  forgot: { title: "Reset your password", description: "Enter the email address you used for your account.", submit: "Request reset link" },
  reset: { title: "Choose a new password", description: "Choose a password you haven't used for this account before.", submit: "Save new password" },
};

export function ConnectionPanel({ busy, initialMode = "login", initialEmail = "", resetToken = "", onClose, onSubmit, onPasswordReset }: Props) {
  const [mode, setMode] = useState<AccountMode>(initialMode);
  const [name, setName] = useState("");
  const [email, setEmail] = useState(initialEmail);
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const submitting = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);
  const firstField = useRef<HTMLInputElement>(null);
  const disabled = busy || pending;
  const close = () => { if (!submitting.current && !busy) onClose(); };
  const dialogRef = useDialogFocus(true, close);
  const copy = COPY[mode];
  const newPassword = mode === "register" || mode === "reset";
  const hasPassword = mode !== "forgot";

  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => { firstField.current?.focus(); }, [mode]);
  useEffect(() => { if (error) errorRef.current?.focus(); }, [error]);

  function changeMode(next: AccountMode) {
    if (disabled) return;
    setMode(next); setError(""); setMessage(""); setPassword(""); setConfirmation(""); setShowPassword(false);
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (disabled || submitting.current) return;
    setError(""); setMessage("");
    if (newPassword) {
      const invalid = passwordValidationMessage(password, confirmation);
      if (invalid) { setError(invalid); return; }
    }
    if (mode === "reset" && !resetToken) { setError("This reset link is incomplete. Request a new link below."); return; }
    const request = new AbortController();
    controller.current = request;
    submitting.current = true;
    setPending(true);
    try {
      if (mode === "forgot") {
        await requestPasswordReset(email.trim(), request.signal);
        request.signal.throwIfAborted();
        setMessage("If an eligible account exists for this email, reset instructions will be sent. Check your inbox and spam folder. If nothing arrives, try again later.");
      } else if (mode === "reset") {
        await confirmPasswordReset(resetToken, password, request.signal);
        request.signal.throwIfAborted();
        await onPasswordReset();
        request.signal.throwIfAborted();
        setPassword(""); setConfirmation(""); setShowPassword(false); setMode("login");
        setMessage("Your password was updated. Sign in with your new password.");
      } else {
        await onSubmit({ intent: mode, name: name.trim(), email: email.trim(), password });
      }
    } catch (failure) {
      if (!request.signal.aborted) setError(accountErrorMessage(failure, mode));
    } finally {
      submitting.current = false;
      if (controller.current === request) {
        controller.current = null;
        if (!request.signal.aborted) setPending(false);
      }
    }
  }

  return <div className="dialog-backdrop" role="presentation" onMouseDown={close}>
    <section ref={dialogRef} tabIndex={-1} className="connection-dialog account-dialog" role="dialog" aria-modal="true" aria-labelledby="connection-title" aria-describedby="connection-description" aria-busy={disabled} onMouseDown={(event) => event.stopPropagation()}>
      <div className="dialog-heading"><div><span className="eyebrow">Your account</span><h2 id="connection-title">{copy.title}</h2><p id="connection-description">{copy.description}</p></div><button type="button" className="icon-button" disabled={disabled} onClick={close} aria-label="Close">×</button></div>
      {mode === "login" || mode === "register" ? <div className="auth-tabs" role="group" aria-label="Account access">
        <button type="button" disabled={disabled} className={mode === "login" ? "active" : ""} aria-pressed={mode === "login"} onClick={() => changeMode("login")}>Sign in</button>
        <button type="button" disabled={disabled} className={mode === "register" ? "active" : ""} aria-pressed={mode === "register"} onClick={() => changeMode("register")}>Create account</button>
      </div> : null}
      <form onSubmit={handleSubmit}>
        {error ? <p id="account-error" ref={errorRef} className="account-feedback account-error" role="alert" tabIndex={-1}>{error}</p> : null}
        {message ? <p className="account-feedback account-success" role="status">{message}</p> : null}
        {mode === "register" ? <label htmlFor="account-name">Name<input id="account-name" ref={firstField} autoComplete="name" required minLength={2} maxLength={120} disabled={disabled} value={name} onChange={(event) => setName(event.target.value)} /></label> : null}
        {mode !== "reset" ? <label htmlFor="account-email">Email<input id="account-email" ref={mode !== "register" ? firstField : undefined} autoComplete="email" autoCapitalize="none" spellCheck={false} required type="email" maxLength={254} disabled={disabled} value={email} onChange={(event) => setEmail(event.target.value)} /></label> : null}
        {hasPassword ? <div className="account-password-field"><label htmlFor="account-password">{newPassword ? "New password" : "Password"}</label><div className="account-password-input"><input id="account-password" ref={mode === "reset" ? firstField : undefined} autoComplete={newPassword ? "new-password" : "current-password"} required type={showPassword ? "text" : "password"} disabled={disabled} aria-describedby={[newPassword ? "password-help" : "", error ? "account-error" : ""].filter(Boolean).join(" ") || undefined} value={password} onChange={(event) => setPassword(event.target.value)} /><button type="button" className="text-button" disabled={disabled} aria-pressed={showPassword} onClick={() => setShowPassword((shown) => !shown)}>{showPassword ? "Hide password" : "Show password"}</button></div>{newPassword ? <p className="account-help" id="password-help">Use at least 10 characters, including a letter and a number.</p> : null}</div> : null}
        {newPassword ? <label htmlFor="account-confirm-password">Confirm new password<input id="account-confirm-password" autoComplete="new-password" required type={showPassword ? "text" : "password"} disabled={disabled} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label> : null}
        {mode === "login" ? <button type="button" className="text-button account-link" disabled={disabled} onClick={() => changeMode("forgot")}>Forgot password?</button> : null}
        {mode === "reset" ? <button type="button" className="text-button account-link" disabled={disabled} onClick={() => changeMode("forgot")}>Request a new reset link</button> : null}
        {mode === "forgot" ? <button type="button" className="text-button account-link" disabled={disabled} onClick={() => changeMode("login")}>Back to sign in</button> : null}
        {pending ? <p className="account-help" role="status">{mode === "forgot" ? "Requesting reset instructions…" : mode === "reset" ? "Updating your password…" : "Signing you in…"}</p> : null}
        <div className="dialog-actions"><button type="button" className="secondary" disabled={disabled} onClick={close}>Cancel</button><button className="primary" disabled={disabled}>{disabled ? "Please wait…" : copy.submit}</button></div>
        <p className="account-help">{mode === "reset" ? "Updating your password signs out your other sessions." : "You can explore the sample without an account. Sign in to save and reopen your own work."}</p>
      </form>
    </section>
  </div>;
}
