import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  cancelJob,
  fetchDataset,
  fetchDatasetPreview,
  describeApiError,
  listDatasets,
  requirePositiveId,
  isAmbiguousJobCreationError,
  isRequestCancelled,
  reconcileCreatedJob,
  startDatasetProfile,
  uploadDataset,
  type CancelJobOptions,
  type Dataset,
  type JobStatus,
} from "../api-client";
import { parseCsvText, type DataRow } from "../data-utils";

export const SAMPLE_ROWS: DataRow[] = [
  { order_id: 100001, order_date: "2024-01-02", customer: "Ana Silva", product: "Notebook Pro", category: "Computers", total: 5299, channel: "Online", state: "SP" },
  { order_id: 100002, order_date: "2024-01-02", customer: "John Santos", product: "Office Chair", category: "Furniture", total: 899.9, channel: "Retail", state: "RJ" },
  { order_id: 100003, order_date: "2024-01-02", customer: "Carla Lima", product: "24-inch Monitor", category: "Computers", total: 699, channel: "Online", state: "MG" },
  { order_id: 100004, order_date: "2024-01-03", customer: "Lucas Rocha", product: "Mechanical Keyboard", category: "Accessories", total: null, channel: "Online", state: "SP" },
  { order_id: 100005, order_date: "2024-01-03", customer: "Marina Costa", product: "Office Desk", category: "Furniture", total: 1299, channel: "Retail", state: "PR" },
  { order_id: 100005, order_date: "2024-01-03", customer: "Marina Costa", product: "Office Desk", category: "Furniture", total: 1299, channel: "Retail", state: "PR" },
];

type TrackJob = (taskId: string, label: string, signal?: AbortSignal) => Promise<JobStatus>;
type CancelTrackedJob = (taskId: string, options: CancelJobOptions & { label?: string }) => Promise<void>;
export type UploadPhase = "idle" | "uploading" | "processing";

function resultIdentifier(result: Record<string, unknown> | null | undefined, key: string) {
  const value = Number(result?.[key]);
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

export type WorkflowStatus = "idle" | "running" | "success" | "failure" | "cancelled";

export function useDatasetUpload(projectId: number | null = null) {
  const [dataset, setDataset] = useState<Dataset | null>(null);
  const [rows, setRows] = useState<DataRow[]>(SAMPLE_ROWS);
  const [schemaColumns, setSchemaColumns] = useState<string[] | null>(null);
  const [datasetName, setDatasetName] = useState("sales_sample.csv");
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState("");
  const [catalogAttempt, setCatalogAttempt] = useState(0);
  const [uploadPhase, setUploadPhase] = useState<UploadPhase>("idle");
  const [uploadProgress, setUploadProgress] = useState(0);
  const [workflowStatus, setWorkflowStatus] = useState<WorkflowStatus>("idle");
  const uploadController = useRef<AbortController | null>(null);
  const activeTaskId = useRef<string | null>(null);
  const activeTaskLabel = useRef("Background job");
  const cancellationPending = useRef(false);
  const columns = useMemo(() => rows.length ? Object.keys(rows[0]) : schemaColumns || [], [rows, schemaColumns]);

  useEffect(() => {
    const controller = new AbortController();
    setDatasets([]);
    setCatalogError("");
    setCatalogLoading(projectId !== null);
    if (projectId === null) return;
    void listDatasets(projectId, controller.signal).then((available) => {
      if (!controller.signal.aborted) setDatasets(available);
    }).catch((error) => {
      if (!controller.signal.aborted) setCatalogError(describeApiError(error));
    }).finally(() => {
      if (!controller.signal.aborted) setCatalogLoading(false);
    });
    return () => controller.abort();
  }, [projectId, catalogAttempt]);

  useEffect(() => {
    if (dataset) setDatasets((available) => available.map((item) => item.id === dataset.id ? dataset : item));
  }, [dataset]);

  useEffect(() => () => {
    uploadController.current?.abort();
    if (activeTaskId.current) void cancelJob(activeTaskId.current, { retryNotFound: true }).catch(() => undefined);
  }, []);

  const beginRequest = useCallback(() => {
    uploadController.current?.abort();
    const controller = new AbortController();
    uploadController.current = controller;
    return controller;
  }, []);

  const reset = useCallback(() => {
    uploadController.current?.abort();
    uploadController.current = null;
    activeTaskId.current = null;
    setDataset(null);
    setSchemaColumns(null);
    setRows(SAMPLE_ROWS.map((row) => ({ ...row })));
    setDatasetName("sales_sample.csv");
    setUploadPhase("idle");
    setUploadProgress(0);
    setWorkflowStatus("idle");
  }, []);

  const openDataset = useCallback(async (datasetId: number) => {
    requirePositiveId(datasetId, "Dataset ID");
    if (!projectId || !datasets.some((item) => item.id === datasetId)) throw new Error("Choose a dataset from the current project.");
    const controller = beginRequest();
    setWorkflowStatus("running");
    try {
      const [current, preview] = await Promise.all([fetchDataset(datasetId, controller.signal), fetchDatasetPreview(datasetId, controller.signal)]);
      controller.signal.throwIfAborted();
      if (current.project_id !== undefined && current.project_id !== projectId) throw new Error("This dataset does not belong to the selected project.");
      setDataset(current); setSchemaColumns(preview.columns); setRows(preview.rows); setDatasetName(current.original_filename);
      setWorkflowStatus("idle");
      return current;
    } catch (error) {
      if (uploadController.current === controller) setWorkflowStatus(controller.signal.aborted ? "cancelled" : "failure");
      throw error;
    } finally {
      if (uploadController.current === controller) uploadController.current = null;
    }
  }, [beginRequest, datasets, projectId]);

  const upload = useCallback(async (file: File, uploadProjectId: number | null, track: TrackJob) => {
    if (uploadProjectId !== null) requirePositiveId(uploadProjectId, "Project ID");
    const controller = beginRequest();
    const previousTaskId = activeTaskId.current;
    activeTaskId.current = null;
    setUploadPhase("uploading");
    setUploadProgress(0);
    setWorkflowStatus("running");
    try {
      if (previousTaskId) await cancelJob(previousTaskId, { retryNotFound: true }).catch(() => undefined);
      controller.signal.throwIfAborted();
      if (uploadProjectId === null) {
        if (!file.name.toLowerCase().endsWith(".csv")) throw new Error("Local mode accepts CSV files. Connect the API for Excel, JSON, or Parquet.");
        const text = await file.text();
        controller.signal.throwIfAborted();
        const parsed = parseCsvText(text);
        setDataset(null); setSchemaColumns(null); setRows(parsed); setDatasetName(file.name);
        setWorkflowStatus("success");
        return { mode: "local" as const };
      }
      const importTaskId = crypto.randomUUID();
      activeTaskId.current = importTaskId;
      activeTaskLabel.current = "Dataset import";
      let importDatasetId: number | null = null;
      let importTrackingId: string = importTaskId;
      try {
        const queued = await uploadDataset(uploadProjectId, file, {
          taskId: importTaskId,
          signal: controller.signal,
          onProgress: (progress) => { if (!controller.signal.aborted) setUploadProgress(progress); },
        });
        controller.signal.throwIfAborted();
        importDatasetId = requirePositiveId(queued.dataset_id, "Dataset ID");
        importTrackingId = queued.task_id;
        activeTaskId.current = importTrackingId;
      } catch (error) {
        if (isRequestCancelled(error) || !isAmbiguousJobCreationError(error)) throw error;
        const recovered = await reconcileCreatedJob(importTaskId, controller.signal);
        controller.signal.throwIfAborted();
        if (!recovered) throw error;
      }
      setUploadPhase("processing");
      const completedImport = await track(importTrackingId, "Dataset import", controller.signal);
      controller.signal.throwIfAborted();
      importDatasetId ??= resultIdentifier(completedImport.result, "dataset_id");
      if (!importDatasetId) throw new Error("The completed import did not identify its dataset.");
      activeTaskId.current = null;
      let current = await fetchDataset(importDatasetId, controller.signal);
      const preview = await fetchDatasetPreview(current.id, controller.signal);
      controller.signal.throwIfAborted();
      setDataset(current); setSchemaColumns(preview.columns); setRows(preview.rows); setDatasetName(current.original_filename);
      setCatalogAttempt((attempt) => attempt + 1);
      const profileTaskId = crypto.randomUUID();
      activeTaskId.current = profileTaskId;
      activeTaskLabel.current = "Dataset profiling";
      let profileTrackingId: string = profileTaskId;
      try {
        const profile = await startDatasetProfile(current.id, profileTaskId, controller.signal);
        controller.signal.throwIfAborted();
        profileTrackingId = profile.task_id;
        activeTaskId.current = profileTrackingId;
      } catch (error) {
        if (isRequestCancelled(error) || !isAmbiguousJobCreationError(error)) throw error;
        const recovered = await reconcileCreatedJob(profileTaskId, controller.signal);
        controller.signal.throwIfAborted();
        if (!recovered) throw error;
      }
      await track(profileTrackingId, "Dataset profiling", controller.signal);
      controller.signal.throwIfAborted();
      activeTaskId.current = null;
      current = await fetchDataset(current.id, controller.signal);
      controller.signal.throwIfAborted();
      setDataset(current);
      setWorkflowStatus("success");
      return { mode: "live" as const };
    } catch (error) {
      if (uploadController.current === controller) setWorkflowStatus(controller.signal.aborted ? "cancelled" : "failure");
      throw error;
    } finally {
      if (uploadController.current === controller) {
        uploadController.current = null;
        activeTaskId.current = null;
        setUploadPhase("idle");
      }
    }
  }, [beginRequest]);

  const datasetId = dataset?.id ?? null;
  const refresh = useCallback(async () => {
    if (!datasetId) return;
    const controller = beginRequest();
    try {
      const [current, preview] = await Promise.all([fetchDataset(datasetId, controller.signal), fetchDatasetPreview(datasetId, controller.signal)]);
      controller.signal.throwIfAborted();
      setDataset(current); setSchemaColumns(preview.columns); setRows(preview.rows);
      setWorkflowStatus("idle");
    } catch (error) {
      if (uploadController.current === controller && !controller.signal.aborted) setWorkflowStatus("failure");
      throw error;
    } finally {
      if (uploadController.current === controller) uploadController.current = null;
    }
  }, [beginRequest, datasetId]);

  const cancelUpload = useCallback(async (cancelTrackedJob?: CancelTrackedJob) => {
    const controller = uploadController.current;
    if (!controller || uploadPhase === "idle") return null;
    const taskId = activeTaskId.current;
    if (cancellationPending.current) return taskId;
    const label = activeTaskLabel.current;
    cancellationPending.current = true;
    controller.abort();
    setWorkflowStatus("cancelled");
    try {
      if (taskId) {
        const cancellation = cancelTrackedJob
          ? cancelTrackedJob(taskId, { retryNotFound: true, label })
          : cancelJob(taskId, { retryNotFound: true });
        await cancellation.catch(() => undefined);
      }
      return taskId;
    } finally {
      cancellationPending.current = false;
    }
  }, [uploadPhase]);

  return {
    dataset, setDataset, rows, setRows, datasetName, columns, upload, refresh, reset,
    uploadPhase, uploadProgress, cancelUpload, workflowStatus, datasets, catalogLoading, catalogError,
    openDataset, reloadCatalog: () => setCatalogAttempt((attempt) => attempt + 1),
  };
}
