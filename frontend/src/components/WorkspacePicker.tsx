import type { Dataset, Project } from "../api-client";
import "./WorkspacePicker.css";

type Props = {
  projects: Project[];
  projectId: number | null;
  datasets: Dataset[];
  datasetId: number | null;
  loading: boolean;
  error: string;
  busy: boolean;
  onProject: (projectId: number) => void;
  onDataset: (datasetId: number) => void;
  onRefresh: () => void;
  onCreateProject: () => void;
  onDeleteProject: () => void;
  onDeleteDataset: () => void;
};

export function WorkspacePicker({ projects, projectId, datasets, datasetId, loading, error, busy, onProject, onDataset, onRefresh, onCreateProject, onDeleteProject, onDeleteDataset }: Props) {
  return <section className="panel workspace-picker" aria-label="Saved workspace">
    <div className="workspace-picker-heading"><div><h2>Saved workspace</h2><p>Open an existing dataset to continue its revision, history, and undo.</p></div><div className="workspace-picker-actions"><button className="secondary" disabled={busy} onClick={onCreateProject}>Create project</button><button className="secondary" disabled={busy || loading || projectId === null} onClick={onRefresh}>Refresh list</button></div></div>
    <div className="field-row">
      <label>Project<select aria-label="Project" value={projectId ?? ""} disabled={busy || projects.length === 0} onChange={(event) => onProject(Number(event.target.value))}>{projectId === null ? <option value="" disabled>No saved projects</option> : null}{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
      <label>Saved dataset<select aria-label="Saved dataset" value={datasets.some((dataset) => dataset.id === datasetId) ? datasetId! : ""} disabled={busy || loading || Boolean(error) || datasets.length === 0} onChange={(event) => { if (event.target.value) onDataset(Number(event.target.value)); }}>
        <option value="" disabled>{loading ? "Loading saved datasets…" : "Choose a saved dataset"}</option>
        {datasets.map((dataset) => <option key={dataset.id} value={dataset.id}>{dataset.original_filename} · Revision {dataset.version} · {dataset.status}</option>)}
      </select></label>
    </div>
    <p className="workspace-picker-status" role={error ? "alert" : "status"}>{error ? `Could not load saved datasets: ${error} Use Refresh list to retry.` : projectId === null ? "You are signed in with no saved projects. Create a project before uploading a dataset." : loading ? "Loading datasets from the selected project…" : datasets.length === 0 ? "This project has no saved datasets yet. Upload a file to get started." : `${datasets.length} saved dataset${datasets.length === 1 ? "" : "s"}. No reupload is needed.`}</p>
    <div className="workspace-picker-actions deletion-actions"><button className="text-button" disabled={busy || projectId === null} onClick={onDeleteProject}>Delete project</button><button className="text-button" disabled={busy || datasetId === null} onClick={onDeleteDataset}>Delete dataset</button></div>
  </section>;
}
