import { useEffect, useRef, useState } from "react";
import { describeApiError, fetchQualityContracts, fetchQualityEvaluations, isAmbiguousJobCreationError, isRequestCancelled, reconcileCreatedJob, saveQualityContract, startQualityEvaluation, type JobStatus, type QualityContract, type QualityEvaluation } from "../api-client";
import { buildQualityRules, newQualityRule, qualityRuleDraft, type QualityRuleDraft } from "../quality-rules";

const RULE_NAMES: Record<QualityRuleDraft["kind"], string> = { not_null: "Required values", unique: "Unique values", range: "Numeric range", accepted_values: "Accepted values", schema: "Expected schema" };
type Props = { datasetId: number | null; version: number; columns: string[]; busy: boolean; onTrack: (taskId: string, label: string) => Promise<JobStatus> };

export function QualityContracts({ datasetId, version, columns, busy, onTrack }: Props) {
  const [contracts, setContracts] = useState<QualityContract[]>([]);
  const [evaluations, setEvaluations] = useState<QualityEvaluation[]>([]);
  const [selectedEvaluation, setSelectedEvaluation] = useState<number | null>(null);
  const [drafts, setDrafts] = useState<QualityRuleDraft[]>([]);
  const [name, setName] = useState("Dataset quality contract");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState(false);
  const [loading, setLoading] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const request = useRef<AbortController | null>(null);
  const latest = contracts.reduce<QualityContract | undefined>((selected, item) => !selected || item.version > selected.version ? item : selected, undefined);
  const latestEvaluation = evaluations.find((item) => item.id === selectedEvaluation) || evaluations[0];
  const editingDisabled = busy || pending || loading;
  useEffect(() => {
    const controller = new AbortController(); request.current = controller;
    setContracts([]); setEvaluations([]); setSelectedEvaluation(null); setDrafts([]); setError(""); setMessage(""); setPending(false);
    if (!datasetId) return () => controller.abort();
    setLoading(true);
    void Promise.all([fetchQualityContracts(datasetId, controller.signal), fetchQualityEvaluations(datasetId, controller.signal)]).then(([savedContracts, savedEvaluations]) => {
      if (controller.signal.aborted) return;
      setContracts(savedContracts); setEvaluations(savedEvaluations);
      const current = savedContracts.reduce<QualityContract | undefined>((selected, item) => !selected || item.version > selected.version ? item : selected, undefined);
      setName(current?.name || "Dataset quality contract");
      setDrafts(current?.rules.map(qualityRuleDraft) || [newQualityRule("rule-1", "")]);
    }).catch((failure) => { if (!controller.signal.aborted && !isRequestCancelled(failure)) setError(describeApiError(failure)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [datasetId, version, attempt]);

  function updateRule(index: number, patch: Partial<QualityRuleDraft>) {
    setDrafts((items) => items.map((item, position) => position === index ? { ...item, ...patch } : item));
  }
  async function save() {
    if (!datasetId || pending || !request.current) return;
    const controller = request.current;
    setPending(true); setError(""); setMessage("");
    try {
      if (!name.trim()) throw new Error("Enter a contract name.");
      const rules = buildQualityRules(drafts.map((rule) => ({ ...rule, column: rule.column || columns[0] || "" })));
      const saved = await saveQualityContract(datasetId, name.trim(), rules, latest?.version || 0, controller.signal);
      if (!controller.signal.aborted) { setContracts((items) => [saved, ...items]); setMessage(`Contract version ${saved.version} saved. Evaluate it against dataset revision ${version}.`); }
    } catch (failure) { if (!controller.signal.aborted) setError(describeApiError(failure)); }
    finally { if (!controller.signal.aborted) setPending(false); }
  }
  async function evaluate() {
    if (!datasetId || !latest || pending || !request.current) return;
    const controller = request.current;
    setPending(true); setError(""); setMessage("");
    try {
      const taskId = crypto.randomUUID(); let trackingId: string = taskId; let needsMonitoring = true;
      try {
        const accepted = await startQualityEvaluation(datasetId, latest.id, version, taskId, controller.signal);
        trackingId = accepted.task_id;
        needsMonitoring = !["completed", "error", "cancelled"].includes(accepted.status);
      } catch (failure) {
        if (!isAmbiguousJobCreationError(failure) || !await reconcileCreatedJob(taskId, controller.signal)) throw failure;
      }
      if (needsMonitoring) await onTrack(trackingId, "Data quality evaluation");
      if (controller.signal.aborted) return;
      const evidence = await fetchQualityEvaluations(datasetId, controller.signal);
      if (!controller.signal.aborted) { setEvaluations(evidence); setSelectedEvaluation(null); }
      if (!controller.signal.aborted) setMessage("Quality evaluation finished. Review the recorded evidence below.");
    } catch (failure) {
      if (!controller.signal.aborted) {
        setError(describeApiError(failure));
        try {
          const evidence = await fetchQualityEvaluations(datasetId, controller.signal);
          if (!controller.signal.aborted) { setEvaluations(evidence); setSelectedEvaluation(null); }
        } catch { /* Preserve the original actionable failure when evidence is also unavailable. */ }
      }
    } finally { if (!controller.signal.aborted) setPending(false); }
  }

  return <article className="panel quality-contracts"><div className="panel-header"><div><h2>Data quality contracts</h2><p>Versioned rules and durable evidence against a specific dataset revision.</p></div>{latest ? <span className="status-pill">Contract v{latest.version}</span> : null}</div>
    {!datasetId ? <div className="empty-state"><strong>Connect and upload a dataset</strong><span>Persistent quality contracts evaluate the full stored dataset in a worker.</span></div> : <div className="quality-contract-content">
      {error ? <p className="form-error" role="alert">{error} <button className="text-button" disabled={pending} onClick={() => setAttempt((value) => value + 1)}>Reload saved contract</button></p> : null}
      {message ? <p role="status">{message}</p> : null}
      {loading ? <p role="status">Loading quality contracts…</p> : <><fieldset className="operation-fields" disabled={editingDisabled || !drafts.length}><label>Contract name<input value={name} onChange={(event) => setName(event.target.value)} maxLength={120} /></label>
        {drafts.map((rule, index) => <div className="quality-rule" key={rule.id}><div className="field-row"><label>Rule {index + 1}<select value={rule.kind} onChange={(event) => updateRule(index, { kind: event.target.value as QualityRuleDraft["kind"] })}>{Object.entries(RULE_NAMES).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
          {rule.kind !== "schema" ? <label>Rule column {index + 1}<select value={rule.column || columns[0] || ""} onChange={(event) => updateRule(index, { column: event.target.value })}>{rule.column && !columns.includes(rule.column) ? <option>{rule.column}</option> : null}{columns.map((column) => <option key={column}>{column}</option>)}</select></label> : null}
          <button className="text-button" disabled={drafts.length === 1} aria-label={`Remove rule ${index + 1}`} onClick={() => setDrafts((items) => items.filter((_, position) => index !== position))}>Remove rule</button></div>
          {rule.kind === "range" ? <div className="field-row"><label>Minimum<input value={rule.min} inputMode="decimal" onChange={(event) => updateRule(index, { min: event.target.value })} placeholder="No lower bound" /></label><label>Maximum<input value={rule.max} inputMode="decimal" onChange={(event) => updateRule(index, { max: event.target.value })} placeholder="No upper bound" /></label></div> : null}
          {rule.kind === "accepted_values" ? <label>Accepted values JSON<textarea rows={2} value={rule.values} onChange={(event) => updateRule(index, { values: event.target.value })} /><small>Preserve types explicitly: ["001", 1, true, null]</small></label> : null}
          {rule.kind === "schema" ? <><label>Expected schema JSON<textarea rows={3} value={rule.schema} onChange={(event) => updateRule(index, { schema: event.target.value })} /><small>Map exact column names to stored dtypes, for example {`{"order_id":"int64"}`}.</small></label><label className="checkbox-label"><input type="checkbox" checked={rule.allowExtra} onChange={(event) => updateRule(index, { allowExtra: event.target.checked })} />Allow additional columns</label></> : null}
        </div>)}
      </fieldset><div className="button-row"><button className="secondary" disabled={editingDisabled || !drafts.length || drafts.length >= 50} onClick={() => setDrafts((items) => [...items, newQualityRule(crypto.randomUUID(), columns[0] || "")])}>Add quality rule</button><button className="primary" disabled={editingDisabled || !drafts.length} onClick={() => void save()}>Save contract version</button><button className="secondary" disabled={editingDisabled || !latest} onClick={() => void evaluate()}>Evaluate saved contract</button></div><p className="quality-footnote">Evaluation uses the latest saved contract. Save draft changes before evaluating. Missing columns fail their rules.</p></>}
      {latestEvaluation ? <section className="quality-evidence" aria-label="Quality evaluation evidence"><label className="engine-field">Evaluation history<select value={latestEvaluation.id} onChange={(event) => setSelectedEvaluation(Number(event.target.value))}>{evaluations.map((item) => <option key={item.id} value={item.id}>{new Date(item.created_at).toLocaleString("en-US")} · Revision {item.dataset_version} · Contract v{item.contract_version} · {item.status}</option>)}</select></label><h3>Evaluation evidence <span className="status-pill">{latestEvaluation.status}</span></h3><p>Contract v{latestEvaluation.contract_version} · Dataset revision {latestEvaluation.dataset_version}{latestEvaluation.stale || latestEvaluation.dataset_version !== version ? " · Historical evidence; evaluate the current revision again." : ""}{latest && latestEvaluation.contract_version !== latest.version ? " · Earlier contract; evaluate the latest saved version." : ""}</p>{latestEvaluation.result ? <><strong>{latestEvaluation.result.passed_count} of {latestEvaluation.result.rule_count} rules passed · {latestEvaluation.result.row_count.toLocaleString("en-US")} rows checked</strong><ul>{latestEvaluation.result.rules.map((rule) => <li key={rule.rule_id}><strong>{rule.passed ? "Pass" : "Fail"} · {rule.column || RULE_NAMES[rule.kind as QualityRuleDraft["kind"]] || rule.kind}</strong><span>{rule.violation_count.toLocaleString("en-US")} violations / {rule.checked_count.toLocaleString("en-US")} checked</span><p>{rule.message}</p></li>)}</ul></> : <p>{latestEvaluation.error_message || "Evidence will appear when the worker completes."} Use Operations monitoring to follow active jobs.</p>}{latestEvaluation.snapshot_sha256 ? <details><summary>Evaluated snapshot checksum</summary><code>{latestEvaluation.snapshot_sha256}</code></details> : null}</section> : null}
    </div>}
  </article>;
}
