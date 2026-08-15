import { useState } from "react";
import { applyDatasetTransformation, fetchTransformationHistory, previewDatasetTransformation, undoDatasetTransformation, type Dataset, type JobStatus, type TransformationPreview, type TransformationResult } from "../api-client";
import { applyLocalOperation, buildApiParameters, type DataOperation, type DataRow } from "../data-utils";

type TrackJob = (taskId: string, label: string) => Promise<JobStatus>;

export function useTransformations(dataset: Dataset | null, rows: DataRow[], setRows: (rows: DataRow[]) => void, refresh: () => Promise<void>, track: TrackJob) {
  const [operation, setOperation] = useState<DataOperation>("drop_duplicates");
  const [column, setColumn] = useState("");
  const [value, setValue] = useState("");
  const [preview, setPreview] = useState<TransformationPreview | null>(null);
  const [history, setHistory] = useState<TransformationResult[]>([]);
  const [localSnapshots, setLocalSnapshots] = useState<DataRow[][]>([]);
  const columns = Object.keys(rows[0] || {});
  const activeColumn = columns.includes(column) ? column : columns[0] || "";

  async function loadHistory() {
    if (!dataset) { setHistory([]); return; }
    setHistory(await fetchTransformationHistory(dataset.id));
  }

  async function previewOperation() {
    if (dataset) {
      const queued = await previewDatasetTransformation(dataset.id, operation, buildApiParameters(operation, { column: activeColumn, value }), dataset.version);
      const completed = await track(queued.task_id, "Transformation preview");
      const result = completed.result as unknown as TransformationPreview;
      if (!result?.before || !result?.after) throw new Error("The transformation preview returned an invalid result.");
      setPreview(result); return result;
    }
    const next = applyLocalOperation(rows, operation, { column: activeColumn, value });
    const result = { before: { rows: rows.length, columns: columns.length, missing_cells: 0 }, after: { rows: next.length, columns: Object.keys(next[0] || {}).length, missing_cells: 0 }, rows: next };
    setPreview(result); return result;
  }

  async function applyOperation() {
    if (dataset) {
      const queued = await applyDatasetTransformation(dataset.id, operation, buildApiParameters(operation, { column: activeColumn, value }), dataset.version);
      await track(queued.task_id, "Dataset transformation");
      await refresh(); await loadHistory(); setPreview(null); return;
    }
    const nextRows = applyLocalOperation(rows, operation, { column: activeColumn, value });
    setLocalSnapshots((items) => [...items, rows.map((row) => ({ ...row }))]);
    setHistory((items) => [{
      id: Date.now(), operation, parameters: buildApiParameters(operation, { column: activeColumn, value }), status: "completed",
      task_id: null, expected_version: items.length + 1, before_rows: rows.length, after_rows: nextRows.length,
      before_columns: columns.length, after_columns: Object.keys(nextRows[0] || {}).length, undone_at: null,
      created_at: new Date().toISOString(),
    }, ...items]);
    setRows(nextRows); setPreview(null);
  }

  async function undo() {
    if (dataset) { await undoDatasetTransformation(dataset.id); await refresh(); await loadHistory(); return; }
    const previous = localSnapshots[localSnapshots.length - 1];
    if (previous) {
      setRows(previous);
      setLocalSnapshots((items) => items.slice(0, -1));
      setHistory((items) => items.slice(1));
    }
  }

  function reset() {
    setPreview(null);
    setHistory([]);
    setLocalSnapshots([]);
  }

  return { operation, setOperation, column: activeColumn, setColumn, value, setValue, preview, history, localSnapshots, previewOperation, applyOperation, undo, loadHistory, reset };
}
