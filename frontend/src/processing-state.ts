import type { JobStatus } from "./api-client";
import type { UploadPhase, WorkflowStatus } from "./hooks/useDatasetUpload";
import { isResumableJobStatus, isTerminalJobStatus } from "./hooks/useProfilingJob";

export type ProcessingState = {
  status: "idle" | "running" | "success" | "failure" | "cancelled" | "monitoring_unavailable";
  progress?: number;
  detail?: string;
};

export function processingState(jobs: JobStatus[], phase: UploadPhase, uploadProgress: number, workflowStatus: WorkflowStatus): ProcessingState {
  const progress = (value: number, maximum = 99) => Number.isFinite(value) ? Math.max(0, Math.min(maximum, Math.round(value))) : 0;
  if (phase === "uploading") return { status: "running", progress: progress(uploadProgress), detail: "Uploading and validating the source file" };
  const running = jobs.find((job) => !isTerminalJobStatus(job.status) && !isResumableJobStatus(job.status));
  if (running) return { status: "running", progress: progress(running.progress), detail: running.status === "CANCELLATION_REQUESTED" ? "Cancellation requested; waiting for the worker" : "Job progress reported by the API" };
  const latest = jobs[0];
  if (latest && isResumableJobStatus(latest.status)) return { status: "monitoring_unavailable", detail: latest.error_message || "Open Monitoring to resume or cancel the job. Its final result is not yet known." };
  if (workflowStatus === "cancelled" || latest?.status === "CANCELLED" || latest?.status === "REVOKED") return { status: "cancelled", detail: latest?.error_message || "The workflow was stopped before completion." };
  if (workflowStatus === "failure" || latest?.status === "FAILURE") return { status: "failure", detail: latest?.error_message || "The last request failed. Review the message above before trying again." };
  if (phase === "processing" || workflowStatus === "running") return { status: "running", detail: "Loading the current dataset state" };
  if (latest?.status === "SUCCESS" || workflowStatus === "success") return { status: "success", progress: 100, detail: "The last request completed successfully." };
  return { status: "idle", detail: "No active job. The loaded preview is ready to inspect." };
}
