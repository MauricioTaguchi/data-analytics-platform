import type { TransformationPreview } from "../api-client";
import { parameterShape, type OperationDefinition, type OperationDraft, type ParameterEntry } from "../operation-contracts";

const COPY: Record<string, { label: string; description: string }> = {
  drop_duplicates: { label: "Remove duplicates", description: "Keeps one occurrence of every identical row." },
  drop_columns: { label: "Remove columns", description: "Removes selected columns from the next dataset version." },
  fill_nulls: { label: "Fill missing values", description: "Replaces missing values using explicitly typed values." },
  rename_columns: { label: "Rename column", description: "Standardizes column names without changing their data." },
  cast_types: { label: "Change data type", description: "Converts columns using the types supported by the selected runtime." },
};

type Props = { live: boolean; busy: boolean; columns: string[]; operation: string; draft: OperationDraft; engine: string; catalog: OperationDefinition[]; catalogError: string; preview: TransformationPreview | null; onOperation: (value: string) => void; onDraft: (value: OperationDraft) => void; onEngine: (value: string) => void; onRetryCatalog: () => void; onPreview: () => void; onApply: () => void };

export function TransformationBuilder(props: Props) {
  const definition = props.catalog.find((item) => item.operation === props.operation);
  const current = COPY[props.operation] || { label: props.operation.replace(/_/g, " "), description: definition?.parameters_schema.description || "Configure parameters according to the operation contract." };
  const shape = definition ? parameterShape(definition) : null;
  const removedRows = props.preview ? props.preview.before.rows - props.preview.after.rows : 0;
  function updateEntry(index: number, patch: Partial<ParameterEntry>) {
    props.onDraft({ ...props.draft, entries: props.draft.entries.map((entry, position) => position === index ? { ...entry, ...patch } : entry) });
  }
  return <section className="work-main">
    <article className="panel transformation-card"><div className="panel-header"><div><h2>Transformation builder</h2><p>{props.live ? "Operations and parameter contracts are provided by the API." : "The local demo supports a bounded set of browser operations."}</p></div></div>
      {!definition ? <div className="empty-state" role="status"><strong>{props.catalogError ? "Operation catalog unavailable" : "Loading operation catalog"}</strong><span>{props.catalogError || "Waiting for the API operation contracts."}</span>{props.catalogError ? <button className="secondary" onClick={props.onRetryCatalog}>Retry catalog</button> : null}</div> : <div className="transform-layout"><div className="operation-list">{props.catalog.map((item) => <button key={item.operation} className={props.operation === item.operation ? "selected" : ""} disabled={props.busy} onClick={() => props.onOperation(item.operation)}>{COPY[item.operation]?.label || item.operation.replace(/_/g, " ")}</button>)}</div>
        <div className="operation-config"><span>Current operation · v{definition.semantic_version}</span><h3>{current.label}</h3><p>{current.description}</p>
          {props.live ? <label className="engine-field">Execution engine<select value={props.engine} disabled={props.busy} onChange={(event) => props.onEngine(event.target.value)}>{Object.keys(definition.engines).map((engine) => <option key={engine}>{engine}</option>)}</select><small>Supported input: {(definition.engines[props.engine] || []).join(", ")}</small></label> : null}
          <fieldset className="operation-fields" disabled={props.busy}>
            {shape?.kind === "empty" ? <p className="operation-scope">All columns · No parameters required</p> : null}
            {shape?.kind === "columns" ? <div className="column-checklist" role="group" aria-label="Columns to remove">{props.columns.map((column) => <label key={column}><input type="checkbox" checked={props.draft.columns.includes(column)} onChange={(event) => props.onDraft({ ...props.draft, columns: event.target.checked ? [...props.draft.columns, column] : props.draft.columns.filter((item) => item !== column) })} />{column}</label>)}</div> : null}
            {shape?.kind === "mapping" ? <>{props.draft.entries.map((entry, index) => <div className="field-row parameter-row" key={index}><label>{index ? `Column ${index + 1}` : "Column"}<select value={entry.column || props.columns[0] || ""} onChange={(event) => updateEntry(index, { column: event.target.value })}>{props.columns.map((column) => <option key={column}>{column}</option>)}</select></label>
              {shape.schema.anyOf ? <label>Value type<select value={entry.type} onChange={(event) => updateEntry(index, { type: event.target.value as ParameterEntry["type"], value: event.target.value === "boolean" ? "false" : "" })}><option value="text">Text</option><option value="number">Number</option><option value="boolean">Boolean</option></select></label> : null}
              <label>{index ? `Parameter ${index + 1}` : "Parameter"}{shape.schema.enum ? <select value={entry.value || shape.schema.enum[0]} onChange={(event) => updateEntry(index, { value: event.target.value })}>{shape.schema.enum.map((value) => <option key={value}>{value}</option>)}</select> : shape.schema.anyOf && entry.type === "boolean" ? <select value={entry.value || "false"} onChange={(event) => updateEntry(index, { value: event.target.value })}><option value="false">false</option><option value="true">true</option></select> : <input value={entry.value} onChange={(event) => updateEntry(index, { value: event.target.value })} inputMode={entry.type === "number" ? "decimal" : "text"} placeholder={props.operation === "rename_columns" ? "new_column_name" : "Replacement value"} />}</label>
              {props.draft.entries.length > 1 ? <button className="text-button" aria-label={`Remove parameter ${index + 1}`} onClick={() => props.onDraft({ ...props.draft, entries: props.draft.entries.filter((_, position) => position !== index) })}>Remove</button> : null}
            </div>)}<button className="text-button" disabled={props.draft.entries.length >= props.columns.length} onClick={() => props.onDraft({ ...props.draft, entries: [...props.draft.entries, { column: props.columns.find((column) => !props.draft.entries.some((entry) => (entry.column || props.columns[0]) === column)) || "", value: "", type: "text" }] })}>Add column parameter</button></> : null}
            {shape?.kind === "json" ? <><label>Parameters JSON<textarea rows={6} value={props.draft.json} onChange={(event) => props.onDraft({ ...props.draft, json: event.target.value })} /></label><details><summary>Parameter contract</summary><pre>{JSON.stringify(definition.parameters_schema, null, 2)}</pre></details></> : null}
          </fieldset>
          <div className="button-row"><button className="secondary" disabled={props.busy} onClick={props.onPreview}>Preview impact</button><button className="primary" disabled={props.busy} onClick={props.onApply}>Apply transformation</button></div>
        </div></div>}
    </article>
    <article className="panel comparison-card"><div className="panel-header"><div><h2>Before and after</h2><p>{props.preview ? "Impact of the proposed operation" : "Create a preview before changing the dataset."}</p></div></div>
      {props.preview ? <div className="comparison"><div><span>Before</span><strong>{props.preview.before.rows} rows</strong><small>{props.preview.before.columns} columns</small></div><div className="comparison-arrow">→</div><div><span>After</span><strong>{props.preview.after.rows} rows</strong><small>{props.preview.after.columns} columns</small></div><div className="impact"><span>Impact</span><strong>{removedRows} {removedRows === 1 ? "row" : "rows"} removed</strong><small>{props.preview.after.missing_cells} {props.preview.after.missing_cells === 1 ? "missing value remains" : "missing values remain"}</small></div></div>
        : <div className="empty-state"><strong>No active preview</strong><span>Select an operation and preview its impact.</span></div>}
    </article>
  </section>;
}
