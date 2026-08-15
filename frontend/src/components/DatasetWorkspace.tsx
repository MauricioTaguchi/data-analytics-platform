import { useMemo, useState, type CSSProperties } from "react";
import { calculateQuality, type DataRow } from "../data-utils";
import { DataPreview } from "./DataPreview";

type Props = { rows: DataRow[]; columns: string[]; live: boolean; progress: number };

export function DatasetWorkspace({ rows, columns, live, progress }: Props) {
  const [search, setSearch] = useState("");
  const visibleColumns = useMemo(() => columns.filter((column) => column.toLowerCase().includes(search.toLowerCase())), [columns, search]);
  const quality = useMemo(() => calculateQuality(rows), [rows]);
  return <section className="overview-grid">
    <DataPreview rows={rows} columns={columns} visibleColumns={visibleColumns} search={search} onSearch={setSearch} />
    <div className="side-stack">
      <article className="panel quality-card"><div className="panel-header"><div><h2>Data quality</h2><p>Calculated from the current preview</p></div></div>
        <div className="quality-body"><div className="score-ring" style={{ "--score": `${quality.score * 3.6}deg` } as CSSProperties}><div><strong>{quality.score}</strong><span>/100</span></div></div>
          <div className="quality-metrics"><p><span>Missing values</span><strong>{quality.missingCount}</strong></p><p><span>Duplicates</span><strong>{quality.duplicateCount}</strong></p><p><span>Columns</span><strong>{columns.length}</strong></p></div></div>
      </article>
      <article className="panel job-card"><div className="panel-header"><div><h2>{progress < 100 ? "Processing dataset" : "Dataset ready"}</h2><p>{live ? "Processed by the distributed worker" : "Computed in the browser"}</p></div><strong>{progress}%</strong></div>
        <div className="progress-track"><div style={{ width: `${progress}%` }} /></div><div className="job-meta"><span>{columns.length} columns analyzed</span><span>{rows.length} preview rows</span></div>
      </article>
    </div>
  </section>;
}
