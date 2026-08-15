import { useCallback, useState } from "react";
import { cancelJob, fetchJobStatus, type JobStatus } from "../api-client";

export type TrackedJob = JobStatus & { label: string; startedAt: string };
const TERMINAL = new Set(["SUCCESS", "FAILURE", "REVOKED"]);

export function useProfilingJob() {
  const [jobs, setJobs] = useState<TrackedJob[]>([]);

  const track = useCallback(async (taskId: string, label: string) => {
    if (taskId === "cached") {
      const cached = { task_id: taskId, status: "SUCCESS", progress: 100, label, startedAt: new Date().toISOString() };
      setJobs((items) => [cached, ...items.filter((item) => item.task_id !== taskId)].slice(0, 12));
      return cached;
    }
    const startedAt = new Date().toISOString();
    setJobs((items) => [{ task_id: taskId, status: "PENDING", progress: 0, label, startedAt }, ...items].slice(0, 12));
    for (let attempt = 0; attempt < 120; attempt += 1) {
      const status = await fetchJobStatus(taskId);
      setJobs((items) => items.map((item) => item.task_id === taskId ? { ...item, ...status } : item));
      if (TERMINAL.has(status.status)) {
        if (status.status !== "SUCCESS") throw new Error(`${label} finished with status ${status.status}.`);
        return status;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 750));
    }
    throw new Error(`${label} exceeded the two-minute monitoring window.`);
  }, []);

  async function cancel(taskId: string) {
    await cancelJob(taskId);
    setJobs((items) => items.map((item) => item.task_id === taskId ? { ...item, status: "REVOKED" } : item));
  }

  return { jobs, track, cancel };
}
