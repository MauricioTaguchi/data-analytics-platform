import { useCallback, useEffect, useState } from "react";
import { applyDatasetTransformation, fetchTransformationHistory, fetchOperationCatalog, describeApiError, isAmbiguousJobCreationError, isRequestCancelled, previewDatasetTransformation, reconcileCreatedJob, type Dataset, type JobStatus, type TransformationPreview, type TransformationResult } from "../api-client";
import { calculateQuality, type DataRow } from "../data-utils";
import { applyLocalParameters, buildOperationParameters, DEMO_OPERATIONS, emptyOperationDraft, type OperationDefinition } from "../operation-contracts";
import { createUndoCoordinator } from "./undo-intent";

type TrackJob = (taskId: string, label: string) => Promise<JobStatus>;

export function useTransformations(dataset: Dataset | null, rows: DataRow[], setRows: (rows: DataRow[]) => void, refresh: () => Promise<void>, track: TrackJob, schemaColumns?: string[], live = Boolean(dataset)) {
  const [operation, setOperation] = useState("drop_duplicates");
  const [draft, setDraft] = useState(emptyOperationDraft);
  const [engine, setEngine] = useState("pandas");
  const [remoteCatalog, setRemoteCatalog] = useState<{ datasetId: number | null; operations: OperationDefinition[] } | null>(null);
  const [catalogError, setCatalogError] = useState("");
  const [catalogAttempt, setCatalogAttempt] = useState(0);
  const [preview, setPreview] = useState<TransformationPreview | null>(null);
  const [history, setHistory] = useState<TransformationResult[]>([]);
  const [localSnapshots, setLocalSnapshots] = useState<DataRow[][]>([]);
  const [undoCoordinator] = useState(createUndoCoordinator);
  const [undoNeedsRecovery, setUndoNeedsRecovery] = useState(false);
  const columns = schemaColumns || Object.keys(rows[0] || {});
  const datasetId = dataset?.id ?? null;
  const catalog = live ? remoteCatalog?.datasetId === datasetId ? remoteCatalog.operations : [] : DEMO_OPERATIONS;
  const definition = catalog.find((item) => item.operation === operation) || catalog[0];
  const selectedEngine = definition?.engines[engine] ? engine : Object.keys(definition?.engines || {})[0];
  useEffect(() => {
    setPreview(null);
    setDraft(emptyOperationDraft());
    if (!live) { setRemoteCatalog(null); setCatalogError(""); return; }
    const controller = new AbortController();
    setCatalogError("");
    setRemoteCatalog(null);
    void fetchOperationCatalog(controller.signal).then((operations) => {
      if (!controller.signal.aborted) setRemoteCatalog({ datasetId, operations });
    }).catch((error) => {
      if (!controller.signal.aborted && !isRequestCancelled(error)) setCatalogError(describeApiError(error));
    });
    return () => controller.abort();
  }, [datasetId, catalogAttempt, live]);

  function parameters() {
    if (!definition) throw new Error("The operation catalog is unavailable. Retry loading it before running a transformation.");
    return buildOperationParameters(definition, draft, columns);
  }

  function changeOperation(next: string) { setOperation(next); setDraft(emptyOperationDraft()); setPreview(null); }


  const loadHistory = useCallback(async (signal?: AbortSignal) => {
    if (!datasetId) { setHistory([]); return; }
    setHistory(await fetchTransformationHistory(datasetId, signal));
  }, [datasetId]);

  useEffect(() => {
    const controller = new AbortController();
    void loadHistory(controller.signal).catch((error) => {
      if (!isRequestCancelled(error)) setHistory([]);
    });
    return () => controller.abort();
  }, [loadHistory]);

  async function previewOperation() {
    if (live && !dataset) throw new Error("Upload a dataset before running a persistent transformation.");
    const payload = parameters();
    if (dataset) {
      const taskId = crypto.randomUUID();
      let trackingId: string = taskId;
      try {
        const queued = await previewDatasetTransformation(dataset.id, definition!.operation, payload, dataset.version, taskId, selectedEngine);
        trackingId = queued.task_id;
      } catch (error) {
        if (!isAmbiguousJobCreationError(error)) throw error;
        const recovered = await reconcileCreatedJob(taskId);
        if (!recovered) throw error;
      }
      const completed = await track(trackingId, "Transformation preview");
      const result = completed.result as unknown as TransformationPreview;
      if (!result?.before || !result?.after) throw new Error("The transformation preview returned an invalid result.");
      setPreview(result); return result;
    }
    const next = applyLocalParameters(rows, definition!.operation, payload);
    const result = {
      before: { rows: rows.length, columns: columns.length, missing_cells: calculateQuality(rows).missingCount },
      after: { rows: next.length, columns: Object.keys(next[0] || {}).length, missing_cells: calculateQuality(next).missingCount },
      rows: next,
    };
    setPreview(result); return result;
  }

  async function applyOperation() {
    if (live && !dataset) throw new Error("Upload a dataset before running a persistent transformation.");
    const payload = parameters();
    if (dataset) {
      const taskId = crypto.randomUUID();
      let trackingId: string = taskId;
      try {
        const queued = await applyDatasetTransformation(dataset.id, definition!.operation, payload, dataset.version, taskId, selectedEngine);
        trackingId = queued.task_id;
      } catch (error) {
        if (!isAmbiguousJobCreationError(error)) throw error;
        const recovered = await reconcileCreatedJob(taskId);
        if (!recovered) throw error;
      }
      await track(trackingId, "Dataset transformation");
      await refresh(); await loadHistory(); setPreview(null); return;
    }
    const nextRows = applyLocalParameters(rows, definition!.operation, payload);
    setLocalSnapshots((items) => [...items, rows.map((row) => ({ ...row }))]);
    setHistory((items) => [{
      id: Date.now(), operation: definition!.operation, operation_version: definition!.semantic_version, parameters: payload, status: "completed",
      task_id: null, expected_version: items.length + 1, before_rows: rows.length, after_rows: nextRows.length,
      before_columns: columns.length, after_columns: Object.keys(nextRows[0] || {}).length, undone_at: null,
      created_at: new Date().toISOString(),
    }, ...items]);
    setRows(nextRows); setPreview(null);
  }

  async function undo() {
    if (dataset) {
      try {
        await undoCoordinator.run(dataset, async () => {
          setPreview(null);
          const results = await Promise.allSettled([refresh(), loadHistory()]);
          const failure = results.find((result) => result.status === "rejected");
          if (failure?.status === "rejected") throw failure.reason;
        });
        setUndoNeedsRecovery(false);
      } catch (error) {
        setUndoNeedsRecovery(undoCoordinator.needsRecovery(dataset.id));
        throw error;
      }
      return;
    }
    const previous = localSnapshots[localSnapshots.length - 1];
    if (previous) {
      setRows(previous);
      setLocalSnapshots((items) => items.slice(0, -1));
      setHistory((items) => items.slice(1));
    }
  }

  const reset = useCallback(() => {
    undoCoordinator.reset();
    setDraft(emptyOperationDraft());
    setUndoNeedsRecovery(false);
    setPreview(null);
    setHistory([]);
    setLocalSnapshots([]);
  }, [undoCoordinator]);

  return { operation: definition?.operation || operation, setOperation: changeOperation, draft, setDraft: (next: typeof draft) => { setDraft(next); setPreview(null); }, engine: selectedEngine, setEngine: (next: string) => { setEngine(next); setPreview(null); }, catalog, catalogError, retryCatalog: () => setCatalogAttempt((attempt) => attempt + 1), preview, history, localSnapshots, previewOperation, applyOperation, undo, undoNeedsRecovery, loadHistory, reset };
}
