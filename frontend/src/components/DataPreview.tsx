import { useMemo, useState } from "react";
import type { DataRow } from "../data-utils";

type Props = { rows: DataRow[]; columns: string[]; visibleColumns: string[]; search: string; onSearch: (value: string) => void };

const PREVIEW_PAGE_SIZE = 10;

export function inferPreviewType(rows: DataRow[], column: string) {
  const types = new Set<string>();
  for (const row of rows) {
    const value = row[column];
    if (value === null || value === undefined) continue;
    types.add(typeof value === "string" ? "text" : typeof value);
    if (types.size > 1) return "mixed";
  }
  return types.values().next().value || "empty";
}

export function previewPage(rows: DataRow[], requestedPage: number) {
  const pageCount = Math.max(1, Math.ceil(rows.length / PREVIEW_PAGE_SIZE));
  const page = Math.max(1, Math.min(requestedPage, pageCount));
  const offset = (page - 1) * PREVIEW_PAGE_SIZE;
  return { page, pageCount, offset, rows: rows.slice(offset, offset + PREVIEW_PAGE_SIZE) };
}

function formatValue(value: DataRow[string] | undefined) {
  if (value === null || value === undefined) return <span className="null-value">null</span>;
  if (typeof value === "number") return String(value);
  return value;
}

export function DataPreview({ rows, columns, visibleColumns, search, onSearch }: Props) {
  const [pagination, setPagination] = useState({ source: rows, page: 1 });
  // Replaced preview data starts at page one without a second render/effect.
  const current = previewPage(rows, pagination.source === rows ? pagination.page : 1);
  const columnTypes = useMemo(
    () => new Map(columns.map((column) => [column, inferPreviewType(rows, column)])),
    [rows, columns],
  );
  const rangeStart = rows.length ? current.offset + 1 : 0;
  const rangeEnd = current.offset + current.rows.length;
  const noMatchingColumns = columns.length > 0 && visibleColumns.length === 0;

  return (
    <article className="panel table-panel preview-panel">
      <div className="panel-header preview-toolbar">
        <div><h2>Data preview <span className="panel-count">{rows.length} rows</span></h2><p>Inspect loaded rows and column values</p></div>
        <label className="preview-search">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4.5 4.5" /></svg>
          <input className="column-search" value={search} onChange={(event) => onSearch(event.target.value)} placeholder="Search columns…" aria-label="Search columns" />
        </label>
      </div>
      {noMatchingColumns ? <div className="empty-state preview-empty" role="status"><strong>No matching columns</strong><span>Try a different column name.</span><button type="button" className="text-button" onClick={() => onSearch("")}>Clear search</button></div>
        : columns.length === 0 ? <div className="empty-state preview-empty"><strong>No preview available</strong><span>Upload a dataset to inspect its rows.</span></div>
          : <div className="table-scroll" tabIndex={0} role="region" aria-label="Scrollable data preview"><table className="preview-table" aria-label="Loaded dataset preview">
            <thead><tr><th className="preview-row-number" scope="col" aria-label="Preview row number">#</th>{visibleColumns.map((column) => <th key={column} scope="col"><span className="column-heading">{column}</span><small className="column-type" title="Type inferred from loaded preview values">{columnTypes.get(column)}</small></th>)}</tr></thead>
            <tbody>{current.rows.length ? current.rows.map((row, index) => <tr key={current.offset + index}><td className="preview-row-number">{current.offset + index + 1}</td>{visibleColumns.map((column) => <td key={column}>{formatValue(row[column])}</td>)}</tr>)
              : <tr><td colSpan={visibleColumns.length + 1}><div className="empty-state preview-empty"><strong>This preview has no rows</strong><span>The column schema is shown above.</span></div></td></tr>}</tbody>
          </table></div>}
      <div className="table-footer">
        <span aria-live="polite">{noMatchingColumns ? `${rows.length} preview rows loaded` : `${rangeStart}–${rangeEnd} of ${rows.length} preview rows`}</span>
        <div className="preview-pagination">
          <span className="pagination-label">{visibleColumns.length === columns.length ? `${columns.length} columns` : `${visibleColumns.length} of ${columns.length} columns`}</span>
          <button type="button" className="pagination-button" aria-label="Previous preview page" disabled={current.page === 1 || noMatchingColumns} onClick={() => setPagination({ source: rows, page: current.page - 1 })}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="m14 6-6 6 6 6" /></svg></button>
          <span className="pagination-label">{current.page} / {current.pageCount}</span>
          <button type="button" className="pagination-button" aria-label="Next preview page" disabled={current.page === current.pageCount || noMatchingColumns} onClick={() => setPagination({ source: rows, page: current.page + 1 })}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="m10 6 6 6-6 6" /></svg></button>
        </div>
      </div>
    </article>
  );
}
