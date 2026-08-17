import type { TrackedJob } from "../hooks/useProfilingJob";
import { isResumableJobStatus, isTerminalJobStatus } from "../hooks/useProfilingJob";

type Props = { jobs: TrackedJob[]; live: boolean; apiStatus: string; onCancel: (taskId: string) => void; onResume: (taskId: string) => void };

export function OperationsMonitor({ jobs, live, apiStatus, onCancel, onResume }: Props) {
  return <section className="page-grid"><article className="panel"><div className="panel-header"><div><h2>Runtime monitoring</h2><p>Worker progress, failures, and cancellation controls</p></div><span className={live ? "status-pill success" : "status-pill"}>{live ? "Authenticated" : "Demo"}</span></div>
    <div className="metric-grid"><div><span>API readiness</span><strong>{apiStatus}</strong></div><div><span>Tracked jobs</span><strong>{jobs.length}</strong></div><div><span>Completed</span><strong>{jobs.filter((job) => job.status === "SUCCESS").length}</strong></div><div><span>Failed</span><strong>{jobs.filter((job) => job.status === "FAILURE").length}</strong></div></div>
  </article><article className="panel"><div className="panel-header"><div><h2>Execution history</h2><p>Most recent asynchronous operations</p></div></div>
    <div className="job-list">{jobs.length ? jobs.map((job) => <div className="job-row" key={`${job.task_id}-${job.label}`}><div><strong>{job.label}</strong><span>{job.task_id}</span></div><div className="job-progress"><div><span style={{ width: `${job.progress}%` }} /></div><small title={job.error_message || undefined}>{job.status}{job.stage ? ` · ${job.stage}` : ""} · {job.progress}%</small></div>{isResumableJobStatus(job.status) ? <button className="secondary" onClick={() => onResume(job.task_id)}>Resume</button> : null}{!isTerminalJobStatus(job.status) ? <button className="secondary" onClick={() => onCancel(job.task_id)}>Cancel</button> : null}</div>) : <div className="empty-state"><strong>No jobs yet</strong><span>Upload or transform a dataset to populate this monitor.</span></div>}</div>
  </article></section>;
}
