import type { Dataset, Project } from "../api-client";
import "./WorkspacePicker.css";

type Props = {
  projects: Project[];
  projectId: number;
  datasets: Dataset[];
  datasetId: number | null;
  loading: boolean;
  error: string;
  busy: boolean;
  onProject: (projectId: number) => void;
  onDataset: (datasetId: number) => void;
  onRefresh: () => void;
};

export function WorkspacePicker({ projects, projectId, datasets, datasetId, loading, error, busy, onProject, onDataset, onRefresh }: Props) {
  return <section className="panel workspace-picker" aria-label="Saved workspace">
    <div className="workspace-picker-heading"><div><h2>Saved workspace</h2><p>Open an existing dataset to continue its revision, history, and undo.</p></div><button className="secondary" disabled={busy || loading} onClick={onRefresh}>Refresh list</button></div>
    <div className="field-row">
      <label>Project<select aria-label="Project" value={projectId} disabled={busy} onChange={(event) => onProject(Number(event.target.value))}>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
      <label>Saved dataset<select aria-label="Saved dataset" value={datasets.some((dataset) => dataset.id === datasetId) ? datasetId! : ""} disabled={busy || loading || Boolean(error) || datasets.length === 0} onChange={(event) => { if (event.target.value) onDataset(Number(event.target.value)); }}>
        <option value="" disabled>{loading ? "Loading saved datasets…" : "Choose a saved dataset"}</option>
        {datasets.map((dataset) => <option key={dataset.id} value={dataset.id}>{dataset.original_filename} · Revision {dataset.version} · {dataset.status}</option>)}
      </select></label>
    </div>
    <p className="workspace-picker-status" role={error ? "alert" : "status"}>{error ? `Could not load saved datasets: ${error} Use Refresh list to retry.` : loading ? "Loading datasets from the selected project…" : datasets.length === 0 ? "This project has no saved datasets yet. Upload a file to get started." : `${datasets.length} saved dataset${datasets.length === 1 ? "" : "s"}. No reupload is needed.`}</p>
  </section>;
}
