import { useEffect, useState } from "react";
import { fetchAccountProfile, type AccountProfile } from "../api-client";
import { accountErrorMessage } from "../account-utils";
import { useDialogFocus } from "../hooks/useDialogFocus";
import "./AccountPanel.css";

type Props = { busy: boolean; onClose: () => void; onSignOut: () => Promise<void>; onResetPassword: (email: string) => void; onDeleteAccount: (profile: AccountProfile) => void };

export function AccountPanel({ busy, onClose, onSignOut, onResetPassword, onDeleteAccount }: Props) {
  const [profile, setProfile] = useState<AccountProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const dialog = useDialogFocus(true, () => { if (!busy) onClose(); });
  useEffect(() => {
    const request = new AbortController();
    setLoading(true); setError("");
    void fetchAccountProfile(request.signal).then((account) => {
      if (!request.signal.aborted) setProfile(account);
    }).catch((failure) => {
      if (!request.signal.aborted) setError(accountErrorMessage(failure, "account"));
    }).finally(() => { if (!request.signal.aborted) setLoading(false); });
    return () => request.abort();
  }, [attempt]);
  return <div className="dialog-backdrop" role="presentation" onMouseDown={() => { if (!busy) onClose(); }}>
    <section ref={dialog} tabIndex={-1} className="connection-dialog account-dialog" role="dialog" aria-modal="true" aria-labelledby="account-title" aria-busy={busy || loading} onMouseDown={(event) => event.stopPropagation()}>
      <div className="dialog-heading"><div><span className="eyebrow">DataFlow</span><h2 id="account-title">Your account</h2><p>Manage access to your saved work.</p></div><button className="icon-button" disabled={busy} onClick={onClose} aria-label="Close">×</button></div>
      <div className="account-body">
        {loading ? <p role="status">Loading your account…</p> : error ? <><p className="account-feedback account-error" role="alert">{error}</p><button className="secondary" onClick={() => setAttempt((value) => value + 1)}>Try again</button></> : profile ? <dl className="account-details"><div><dt>Name</dt><dd>{profile.name}</dd></div><div><dt>Email</dt><dd>{profile.email}</dd></div></dl> : null}
        <p className="account-help">Your projects stay saved when you sign out. On a shared computer, sign out when you're finished.</p>
        <div className="dialog-actions"><button className="secondary" disabled={busy || loading || !profile} onClick={() => { if (profile) onResetPassword(profile.email); }}>Reset password</button><button className="primary" disabled={busy} onClick={() => void onSignOut()}>{busy ? "Signing out…" : "Sign out"}</button></div>
        <button className="text-button account-link" disabled={busy || loading || !profile} onClick={() => { if (profile) onDeleteAccount(profile); }}>Delete account</button>
      </div>
    </section>
  </div>;
}
