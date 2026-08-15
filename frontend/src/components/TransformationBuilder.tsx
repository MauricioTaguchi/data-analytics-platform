import type { TransformationPreview } from "../api-client";
import type { DataOperation } from "../data-utils";

const COPY: Record<DataOperation, { label: string; description: string }> = {
  drop_duplicates: { label: "Remove duplicates", description: "Keeps one occurrence of every identical row." },
  fill_nulls: { label: "Fill missing values", description: "Replaces missing values in the selected column." },
  rename_columns: { label: "Rename column", description: "Standardizes a column name without changing its data." },
  cast_types: { label: "Change data type", description: "Converts a column to text or a number." },
};

type Props = { live: boolean; busy: boolean; columns: string[]; operation: DataOperation; column: string; value: string; preview: TransformationPreview | null; onOperation: (value: DataOperation) => void; onColumn: (value: string) => void; onValue: (value: string) => void; onPreview: () => void; onApply: () => void };

export function TransformationBuilder(props: Props) {
  const current = COPY[props.operation];
  return <section className="work-main">
    <article className="panel transformation-card"><div className="panel-header"><div><h2>Transformation builder</h2><p>{props.live ? "Preview and execution are version-controlled by the API." : "Explore transformations locally before connecting the API."}</p></div></div>
      <div className="transform-layout"><div className="operation-list">{(Object.keys(COPY) as DataOperation[]).map((key) => <button key={key} className={props.operation === key ? "selected" : ""} onClick={() => props.onOperation(key)}>{COPY[key].label}</button>)}</div>
        <div className="operation-config"><span>Current operation</span><h3>{current.label}</h3><p>{current.description}</p>
          <div className="field-row"><label>Column<select value={props.column} onChange={(event) => props.onColumn(event.target.value)} disabled={props.operation === "drop_duplicates"}>{props.columns.map((item) => <option key={item}>{item}</option>)}</select></label>
            <label>Parameter{props.operation === "cast_types" ? <select value={props.value || "text"} onChange={(event) => props.onValue(event.target.value)}><option value="text">text</option><option value="number">number</option></select> : <input value={props.value} onChange={(event) => props.onValue(event.target.value)} disabled={props.operation === "drop_duplicates"} placeholder={props.operation === "rename_columns" ? "new_column_name" : props.operation === "fill_nulls" ? "0" : "not required"} />}</label></div>
          <div className="button-row"><button className="secondary" disabled={props.busy} onClick={props.onPreview}>Preview impact</button><button className="primary" disabled={props.busy} onClick={props.onApply}>Apply transformation</button></div>
        </div></div>
    </article>
    <article className="panel comparison-card"><div className="panel-header"><div><h2>Before and after</h2><p>{props.preview ? "Impact of the proposed operation" : "Create a preview before changing the dataset."}</p></div></div>
      {props.preview ? <div className="comparison"><div><span>Before</span><strong>{props.preview.before.rows} rows</strong><small>{props.preview.before.columns} columns</small></div><div className="comparison-arrow">→</div><div><span>After</span><strong>{props.preview.after.rows} rows</strong><small>{props.preview.after.columns} columns</small></div><div className="impact"><span>Impact</span><strong>{props.preview.before.rows - props.preview.after.rows} rows removed</strong><small>{props.preview.after.missing_cells} missing values remain</small></div></div>
        : <div className="empty-state"><strong>No active preview</strong><span>Select an operation and preview its impact.</span></div>}
    </article>
  </section>;
}
