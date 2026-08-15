import type { TransformationResult } from "../api-client";

type Props = { history: TransformationResult[]; sourceName: string; live: boolean; canUndo: boolean; busy: boolean; onUndo: () => void };

export function LineageTimeline({ history, sourceName, live, canUndo, busy, onUndo }: Props) {
  return <article className="panel lineage-card"><div className="panel-header"><div><h2>Lineage and history</h2><p>{live ? "Persisted, versioned API history." : "History for this browser session."}</p></div></div>
    <div className="timeline">{history.map((item, index) => <div className="timeline-item" key={item.id}><span className={index === 0 ? "timeline-dot active" : "timeline-dot"} /><div><small>{new Date(item.created_at).toLocaleString("en-US")}</small><strong>{item.operation.replace(/_/g, " ")}</strong><p>Version {item.expected_version} · {item.status}</p><span className="row-delta">{item.before_rows} → {item.after_rows} rows</span></div></div>)}
      <div className="timeline-item"><span className={history.length ? "timeline-dot" : "timeline-dot active"} /><div><small>Source</small><strong>Original dataset</strong><p>{sourceName}</p></div></div></div>
    <button className="secondary undo-button" disabled={!canUndo || busy} onClick={onUndo}>Undo latest transformation</button>
  </article>;
}
