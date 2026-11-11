import { useRef, useState, type FormEvent } from "react";
import { deletionErrorMessage, type DeletionTarget } from "../lifecycle-api";
import { useDialogFocus } from "../hooks/useDialogFocus";
import "./DeleteResourceDialog.css";

type Props = {
  target: DeletionTarget;
  onClose: () => void;
  onDelete: (confirmation: string, password: string) => Promise<void>;
};

export function DeleteResourceDialog({ target, onClose, onDelete }: Props) {
  const [confirmation, setConfirmation] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const errorRef = useRef<HTMLParagraphElement>(null);
  const pending = useRef(false);
  const dialog = useDialogFocus(true, () => { if (!pending.current) onClose(); });

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (pending.current || confirmation !== "DELETE" || (target.scope === "account" && !password)) return;
    pending.current = true;
    setSubmitting(true); setError("");
    try {
      await onDelete(confirmation, password);
    } catch (failure) {
      setError(deletionErrorMessage(failure, target.scope));
      // Do not keep the password around after a rejected or uncertain request.
      setPassword("");
      requestAnimationFrame(() => errorRef.current?.focus());
    } finally {
      pending.current = false;
      setSubmitting(false);
    }
  }

  return <div className="dialog-backdrop" role="presentation" onMouseDown={() => { if (!pending.current) onClose(); }}>
    <section ref={dialog} tabIndex={-1} className="connection-dialog deletion-dialog" role="dialog" aria-modal="true" aria-labelledby="deletion-title" aria-describedby="deletion-impact deletion-retention" aria-busy={submitting} onMouseDown={(event) => event.stopPropagation()}>
      <div className="dialog-heading"><div><span className="eyebrow">Confirm deletion</span><h2 id="deletion-title">Delete {target.scope}?</h2></div><button className="icon-button" disabled={submitting} aria-label="Close deletion dialog" onClick={onClose}>×</button></div>
      <form className="deletion-body" onSubmit={(event) => void submit(event)}>
        <p className="deletion-name">{target.name}</p>
        <p id="deletion-impact">{target.scope === "account"
          ? "Your account will close and all sessions will lose access. Your projects and datasets will be removed from the workspace."
          : target.scope === "project" ? "This project and its datasets will be removed from your workspace."
            : "This dataset will be removed from your workspace, including access to its saved versions and reports."} You cannot undo this action in the application.</p>
        <p id="deletion-retention" className="deletion-retention">File cleanup runs in the background. Audit records, including source names, transformation settings and quality evidence, and existing backups remain subject to the operator's retention policy. This is not complete data erasure.</p>
        <p className="deletion-retention">Active jobs must finish or be cancelled before deletion.</p>
        {target.scope === "account" ? <label>Current password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} disabled={submitting} required /></label> : null}
        <label>Type DELETE to confirm<input aria-describedby="deletion-impact deletion-retention" autoComplete="off" spellCheck={false} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} disabled={submitting} required /></label>
        {error ? <p ref={errorRef} className="deletion-error" role="alert" tabIndex={-1}>{error}</p> : null}
        <div className="dialog-actions"><button type="button" className="secondary" data-dialog-initial-focus="true" disabled={submitting} onClick={onClose}>Keep {target.scope}</button><button type="submit" className="danger-button" disabled={submitting || confirmation !== "DELETE" || (target.scope === "account" && !password)}>{submitting ? "Deleting…" : `Delete ${target.scope}`}</button></div>
      </form>
    </section>
  </div>;
}
