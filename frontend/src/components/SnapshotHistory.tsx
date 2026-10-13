import { useEffect, useRef, useState } from "react";
import { describeApiError, fetchDatasetSnapshots, isRequestCancelled, pinDatasetSnapshot, type DatasetSnapshot } from "../api-client";

export function SnapshotHistory({ datasetId, version, busy }: { datasetId: number; version: number; busy: boolean }) {
  const [snapshots, setSnapshots] = useState<DatasetSnapshot[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const generation = useRef(0);
  useEffect(() => {
    generation.current += 1;
    const controller = new AbortController();
    setLoading(true); setError(""); setSnapshots([]); setSaving(null);
    void fetchDatasetSnapshots(datasetId, controller.signal).then((items) => {
      if (!controller.signal.aborted) setSnapshots(items);
    }).catch((failure) => {
      if (!controller.signal.aborted && !isRequestCancelled(failure)) setError(describeApiError(failure));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); generation.current += 1; };
  }, [datasetId, version, attempt]);

  async function togglePin(snapshot: DatasetSnapshot) {
    if (!snapshot.id || saving) return;
    const requestGeneration = generation.current;
    setSaving(snapshot.id); setError("");
    try {
      const updated = await pinDatasetSnapshot(datasetId, snapshot.id, !snapshot.pinned);
      if (generation.current === requestGeneration) setSnapshots((items) => items.map((item) => item.id === updated.id ? updated : item));
    } catch (failure) { if (generation.current === requestGeneration) setError(describeApiError(failure)); }
    finally { if (generation.current === requestGeneration) setSaving(null); }
  }

  return <article className="panel snapshot-card"><div className="panel-header"><div><h2>Snapshot retention</h2><p>Pin versions to protect them from automatic expiration.</p></div></div>
    <div className="snapshot-content">{error ? <p role="alert">{error} <button className="text-button" onClick={() => setAttempt((value) => value + 1)}>Refresh snapshots</button></p> : null}
      {loading ? <p role="status">Loading snapshots…</p> : snapshots.length ? <ul className="snapshot-list">{snapshots.map((snapshot, index) => <li key={snapshot.id || `legacy-${index}`}><div><strong>{snapshot.version === null ? "Legacy snapshot" : `Snapshot ${snapshot.version}`}</strong><span className="status-pill">{snapshot.active ? "Current" : snapshot.availability === "EXPIRED" ? "Expired" : snapshot.restorable ? "Undo target" : "Retained"}</span></div><p>{snapshot.integrity === "verified" ? "SHA-256 recorded" : "Legacy · no checksum recorded"}{snapshot.size_bytes !== null ? ` · ${snapshot.size_bytes.toLocaleString("en-US")} bytes` : ""}</p>{snapshot.sha256 ? <details><summary>Content checksum</summary><code>{snapshot.sha256}</code></details> : null}{snapshot.expired_at ? <small>Expired {new Date(snapshot.expired_at).toLocaleString("en-US")}</small> : null}{snapshot.id && snapshot.availability === "AVAILABLE" ? <button className="secondary" disabled={busy || saving !== null} aria-pressed={snapshot.pinned} onClick={() => void togglePin(snapshot)}>{saving === snapshot.id ? "Saving…" : snapshot.pinned ? "Unpin snapshot" : "Pin snapshot"}</button> : null}</li>)}</ul> : <p>No snapshots are available.</p>}
      <p className="quality-footnote">Undo restores the latest eligible transformation. Pinning protects a file from expiration; it does not enable arbitrary version restoration. Recorded checksums are verified against the file before undo.</p>
    </div>
  </article>;
}
