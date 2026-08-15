import type { DataRow } from "../data-utils";

type Props = { rows: DataRow[]; columns: string[]; visibleColumns: string[]; search: string; onSearch: (value: string) => void };

function formatValue(value: DataRow[string]) {
  if (value === null) return <span className="null-value">null</span>;
  if (typeof value === "number") return new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(value);
  return value;
}

export function DataPreview({ rows, columns, visibleColumns, search, onSearch }: Props) {
  return (
    <article className="panel table-panel">
      <div className="panel-header">
        <div><h2>Data preview</h2><p>First {Math.min(rows.length, 100)} rows</p></div>
        <input className="column-search" value={search} onChange={(event) => onSearch(event.target.value)} placeholder="Search columns" aria-label="Search columns" />
      </div>
      <div className="table-scroll"><table>
        <thead><tr>{visibleColumns.map((column) => <th key={column}>{column}<small>{typeof rows[0]?.[column]}</small></th>)}</tr></thead>
        <tbody>{rows.slice(0, 6).map((row, index) => <tr key={index}>{visibleColumns.map((column) => <td key={column}>{formatValue(row[column])}</td>)}</tr>)}</tbody>
      </table></div>
      <div className="table-footer">Showing {Math.min(rows.length, 6)} of {rows.length} rows <span>{columns.length} columns</span></div>
    </article>
  );
}
