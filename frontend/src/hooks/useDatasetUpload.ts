import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  cancelJob,
  fetchDataset,
  fetchDatasetPreview,
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
  return Number.isInteger(value) && value > 0 ? value : null;
}

export function useDatasetUpload() {
  const [dataset, setDataset] = useState<Dataset | null>(null);
  const [rows, setRows] = useState<DataRow[]>(SAMPLE_ROWS);
  const [datasetName, setDatasetName] = useState("sales_sample.csv");
  const [uploadPhase, setUploadPhase] = useState<UploadPhase>("idle");
  const [uploadProgress, setUploadProgress] = useState(0);
  const uploadController = useRef<AbortController | null>(null);
  const activeTaskId = useRef<string | null>(null);
  const activeTaskLabel = useRef("Background job");
  const cancellationPending = useRef(false);
  const columns = useMemo(() => Object.keys(rows[0] || {}), [rows]);

  useEffect(() => () => {
    uploadController.current?.abort();
    if (activeTaskId.current) void cancelJob(activeTaskId.current, { retryNotFound: true }).catch(() => undefined);
  }, []);

  const reset = useCallback(() => {
    uploadController.current?.abort();
    uploadController.current = null;
    activeTaskId.current = null;
    setDataset(null);
    setRows(SAMPLE_ROWS.map((row) => ({ ...row })));
    setDatasetName("sales_sample.csv");
    setUploadPhase("idle");
    setUploadProgress(0);
  }, []);

  const upload = useCallback(async (file: File, projectId: number | null, track: TrackJob) => {
    if (!projectId) {
      if (!file.name.toLowerCase().endsWith(".csv")) throw new Error("Local mode accepts CSV files. Connect the API for Excel, JSON, or Parquet.");
      const parsed = parseCsvText(await file.text());
      setDataset(null); setRows(parsed); setDatasetName(file.name);
      return { mode: "local" as const };
    }
    uploadController.current?.abort();
    const previousTaskId = activeTaskId.current;
    activeTaskId.current = null;
    if (previousTaskId) await cancelJob(previousTaskId, { retryNotFound: true }).catch(() => undefined);
    const controller = new AbortController();
    uploadController.current = controller;
    setUploadPhase("uploading");
    setUploadProgress(0);
    try {
      const importTaskId = crypto.randomUUID();
      activeTaskId.current = importTaskId;
      activeTaskLabel.current = "Dataset import";
      let importDatasetId: number | null = null;
      let importTrackingId = importTaskId;
      try {
        const queued = await uploadDataset(projectId, file, {
          taskId: importTaskId,
          signal: controller.signal,
          onProgress: setUploadProgress,
        });
        importDatasetId = queued.dataset_id;
        importTrackingId = queued.task_id;
        activeTaskId.current = importTrackingId;
      } catch (error) {
        if (isRequestCancelled(error) || !isAmbiguousJobCreationError(error)) throw error;
        const recovered = await reconcileCreatedJob(importTaskId, controller.signal);
        if (!recovered) throw error;
      }
      setUploadPhase("processing");
      const completedImport = await track(importTrackingId, "Dataset import", controller.signal);
      importDatasetId ??= resultIdentifier(completedImport.result, "dataset_id");
      if (!importDatasetId) throw new Error("The completed import did not identify its dataset.");
      activeTaskId.current = null;
      let current = await fetchDataset(importDatasetId, controller.signal);
      const preview = await fetchDatasetPreview(current.id, controller.signal);
      setDataset(current); setRows(preview.rows); setDatasetName(current.original_filename);
      const profileTaskId = crypto.randomUUID();
      activeTaskId.current = profileTaskId;
      activeTaskLabel.current = "Dataset profiling";
      let profileTrackingId = profileTaskId;
      try {
        const profile = await startDatasetProfile(current.id, profileTaskId, controller.signal);
        profileTrackingId = profile.task_id;
        activeTaskId.current = profileTrackingId;
      } catch (error) {
        if (isRequestCancelled(error) || !isAmbiguousJobCreationError(error)) throw error;
        const recovered = await reconcileCreatedJob(profileTaskId, controller.signal);
        if (!recovered) throw error;
      }
      await track(profileTrackingId, "Dataset profiling", controller.signal);
      activeTaskId.current = null;
      current = await fetchDataset(current.id, controller.signal);
      setDataset(current);
      return { mode: "live" as const };
    } finally {
      if (uploadController.current === controller) {
        uploadController.current = null;
        activeTaskId.current = null;
        setUploadPhase("idle");
      }
    }
  }, []);

  const datasetId = dataset?.id ?? null;
  const refresh = useCallback(async () => {
    if (!datasetId) return;
    const [current, preview] = await Promise.all([fetchDataset(datasetId), fetchDatasetPreview(datasetId)]);
    setDataset(current); setRows(preview.rows);
  }, [datasetId]);

  const cancelUpload = useCallback(async (cancelTrackedJob?: CancelTrackedJob) => {
    const controller = uploadController.current;
    if (!controller || uploadPhase === "idle") return null;
    const taskId = activeTaskId.current;
    if (cancellationPending.current) return taskId;
    const label = activeTaskLabel.current;
    cancellationPending.current = true;
    controller.abort();
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
    dataset,
    setDataset,
    rows,
    setRows,
    datasetName,
    columns,
    upload,
    refresh,
    reset,
    uploadPhase,
    uploadProgress,
    cancelUpload,
  };
}
