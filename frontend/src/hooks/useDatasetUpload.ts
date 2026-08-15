import { useMemo, useState } from "react";
import { fetchDataset, fetchDatasetPreview, startDatasetProfile, uploadDataset, type Dataset, type JobStatus } from "../api-client";
import { parseCsvText, type DataRow } from "../data-utils";

export const SAMPLE_ROWS: DataRow[] = [
  { order_id: 100001, order_date: "2024-01-02", customer: "Ana Silva", product: "Notebook Pro", category: "Computers", total: 5299, channel: "Online", state: "SP" },
  { order_id: 100002, order_date: "2024-01-02", customer: "John Santos", product: "Office Chair", category: "Furniture", total: 899.9, channel: "Retail", state: "RJ" },
  { order_id: 100003, order_date: "2024-01-02", customer: "Carla Lima", product: "24-inch Monitor", category: "Computers", total: 699, channel: "Online", state: "MG" },
  { order_id: 100004, order_date: "2024-01-03", customer: "Lucas Rocha", product: "Mechanical Keyboard", category: "Accessories", total: null, channel: "Online", state: "SP" },
  { order_id: 100005, order_date: "2024-01-03", customer: "Marina Costa", product: "Office Desk", category: "Furniture", total: 1299, channel: "Retail", state: "PR" },
  { order_id: 100005, order_date: "2024-01-03", customer: "Marina Costa", product: "Office Desk", category: "Furniture", total: 1299, channel: "Retail", state: "PR" },
];

type TrackJob = (taskId: string, label: string) => Promise<JobStatus>;

export function useDatasetUpload() {
  const [dataset, setDataset] = useState<Dataset | null>(null);
  const [rows, setRows] = useState<DataRow[]>(SAMPLE_ROWS);
  const [datasetName, setDatasetName] = useState("sales_sample.csv");
  const columns = useMemo(() => Object.keys(rows[0] || {}), [rows]);

  function reset() { setDataset(null); setRows(SAMPLE_ROWS.map((row) => ({ ...row }))); setDatasetName("sales_sample.csv"); }

  async function upload(file: File, projectId: number | null, track: TrackJob) {
    if (!projectId) {
      if (!file.name.toLowerCase().endsWith(".csv")) throw new Error("Local mode accepts CSV files. Connect the API for Excel, JSON, or Parquet.");
      const parsed = parseCsvText(await file.text());
      setDataset(null); setRows(parsed); setDatasetName(file.name);
      return { mode: "local" as const };
    }
    const queued = await uploadDataset(projectId, file);
    await track(queued.task_id, "Dataset import");
    let current = await fetchDataset(queued.dataset_id);
    const preview = await fetchDatasetPreview(current.id);
    setDataset(current); setRows(preview.rows); setDatasetName(current.original_filename);
    const profile = await startDatasetProfile(current.id);
    await track(profile.task_id, "Dataset profiling");
    current = await fetchDataset(current.id);
    setDataset(current);
    return { mode: "live" as const };
  }

  async function refresh() {
    if (!dataset) return;
    const [current, preview] = await Promise.all([fetchDataset(dataset.id), fetchDatasetPreview(dataset.id)]);
    setDataset(current); setRows(preview.rows);
  }

  return { dataset, setDataset, rows, setRows, datasetName, columns, upload, refresh, reset };
}
