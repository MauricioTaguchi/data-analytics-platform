import { useMemo, useState, type CSSProperties } from "react";
import { calculateQuality, type DataRow } from "../data-utils";
import { DataPreview } from "./DataPreview";

type Props = { rows: DataRow[]; columns: string[]; live: boolean; progress: number };

export function DatasetWorkspace({ rows, columns, live, progress }: Props) {
  const [search, setSearch] = useState("");
  const visibleColumns = useMemo(() => columns.filter((column) => column.toLowerCase().includes(search.toLowerCase())), [columns, search]);
  const quality = useMemo(() => calculateQuality(rows), [rows]);
  const percent = Number.isFinite(progress) ? Math.max(0, Math.min(100, Math.round(progress))) : 0;
  const hasPreview = rows.length > 0;
  return <section className="overview-grid">
    <DataPreview rows={rows} columns={columns} visibleColumns={visibleColumns} search={search} onSearch={setSearch} />
    <div className="side-stack workspace-summary">
      <article className="panel quality-card"><div className="panel-header"><div><h2>Preview quality</h2><p>Based on the loaded preview</p></div><span className="card-eyebrow">Quality score</span></div>
        <div className="quality-body"><div className="score-ring" role="img" aria-label={hasPreview ? `Preview quality score: ${quality.score} out of 100` : "Quality score unavailable without preview rows"} style={{ "--score": `${hasPreview ? quality.score * 3.6 : 0}deg` } as CSSProperties}><div><strong>{hasPreview ? quality.score : "—"}</strong><span>out of 100</span></div></div>
          <div className="quality-metrics"><p><span><i className="metric-dot warning" />Missing values</span><strong>{quality.missingCount}</strong></p><p><span><i className="metric-dot violet" />Duplicate rows</span><strong>{quality.duplicateCount}</strong></p><p><span><i className="metric-dot neutral" />Columns</span><strong>{columns.length}</strong></p></div></div>
        <p className="quality-footnote">{hasPreview ? `Heuristic score across ${rows.length} loaded preview rows.` : "Load preview rows to calculate quality."}</p>
      </article>
      <article className="panel job-card"><div className="panel-header"><div><h2>Processing status</h2><p>{live ? "Background worker" : "Browser session"}</p></div><span className={`processing-badge ${percent === 100 ? "complete" : "active"}`}>{percent === 100 ? "Complete" : "In progress"}</span></div>
        <div className="processing-heading"><span className="processing-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">{percent === 100 ? <path d="m5 12 4 4L19 6" /> : <><path d="m13 2-9 12h7l-1 8 10-13h-7z" /></>}</svg></span><div><strong>{percent === 100 ? "Ready to explore" : "Processing your request"}</strong><p className="processing-copy">{live ? "Job progress reported by the API" : "Sample data is processed locally"}</p></div><strong className="processing-percent">{percent}%</strong></div>
        <div className="progress-track" role="progressbar" aria-label="Dataset processing progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}><div style={{ width: `${percent}%` }} /></div><div className="job-meta"><span>{columns.length} preview columns</span><span>{rows.length} loaded rows</span></div>
      </article>
    </div>
  </section>;
}
