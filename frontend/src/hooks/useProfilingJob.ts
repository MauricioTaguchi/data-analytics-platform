import { useCallback, useEffect, useRef, useState } from "react";
import axios from "axios";
import { cancelJob, describeApiError, fetchJobStatus, type CancelJobOptions, type JobStatus } from "../api-client";

export type TrackedJob = JobStatus & { label: string; startedAt: string };
const TERMINAL_JOB_STATUSES = new Set(["SUCCESS", "FAILURE", "CANCELLED", "REVOKED"]);
const POLL_INTERVAL_MS = 750;
const POLL_TIMEOUT_MS = 720_000;

export class JobPollingTimeoutError extends Error {}

export function isTerminalJobStatus(status: string) {
  return TERMINAL_JOB_STATUSES.has(status);
}

export function isResumableJobStatus(status: string) {
  return status === "TIMEOUT" || status === "MONITORING_ERROR";
}

export function monitoringFailureStatus(error: unknown): Pick<JobStatus, "status" | "stage" | "error_message"> {
  if (error instanceof JobPollingTimeoutError) {
    return { status: "TIMEOUT", stage: "monitoring_timeout", error_message: error.message };
  }
  return { status: "MONITORING_ERROR", stage: "monitoring_error", error_message: describeApiError(error) };
}

function waitForNextPoll(signal: AbortSignal, delayMs = POLL_INTERVAL_MS) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Polling cancelled", "AbortError"));
      return;
    }
    const onAbort = () => {
      globalThis.clearTimeout(timeoutId);
      reject(new DOMException("Polling cancelled", "AbortError"));
    };
    const timeoutId = globalThis.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, delayMs);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

type PollJobOptions = {
  taskId: string;
  label: string;
  signal: AbortSignal;
  onStatus?: (status: JobStatus) => void;
  fetchStatus?: typeof fetchJobStatus;
  wait?: (signal: AbortSignal, delayMs?: number) => Promise<void>;
  now?: () => number;
  timeoutMs?: number;
};

export async function pollJobUntilTerminal({
  taskId,
  label,
  signal,
  onStatus,
  fetchStatus = fetchJobStatus,
  wait = waitForNextPoll,
  now = Date.now,
  timeoutMs = POLL_TIMEOUT_MS,
}: PollJobOptions) {
  const deadline = now() + timeoutMs;
  let transientFailures = 0;
  while (now() < deadline) {
    let status: JobStatus;
    try {
      status = await fetchStatus(taskId, signal);
      transientFailures = 0;
    } catch (error) {
      if (signal.aborted) throw error;
      const retryable = axios.isAxiosError(error)
        && (!error.response || [408, 425, 429].includes(error.response.status) || error.response.status >= 500);
      if (!retryable) throw error;
      transientFailures += 1;
      await wait(signal, Math.min(POLL_INTERVAL_MS * (2 ** transientFailures), 10_000));
      continue;
    }
    const normalized = { ...status, progress: Math.max(0, Math.min(100, status.progress)) };
    onStatus?.(normalized);
    if (isTerminalJobStatus(normalized.status)) {
      if (normalized.status !== "SUCCESS") {
        throw new Error(normalized.error_message || `${label} finished with status ${normalized.status}.`);
      }
      return normalized;
    }
    await wait(signal);
  }
  const minutes = Math.max(1, Math.ceil(timeoutMs / 60_000));
  throw new JobPollingTimeoutError(`${label} did not finish within ${minutes} minutes. Resume monitoring or request cancellation.`);
}

export function useProfilingJob() {
  const [jobs, setJobs] = useState<TrackedJob[]>([]);
  const controllers = useRef(new Map<string, AbortController>());
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    const activeControllers = controllers.current;
    return () => {
      mounted.current = false;
      activeControllers.forEach((controller) => controller.abort());
      activeControllers.clear();
    };
  }, []);

  const track = useCallback(async (taskId: string, label: string, externalSignal?: AbortSignal) => {
    if (taskId === "cached") {
      const cached: TrackedJob = { task_id: taskId, status: "SUCCESS", progress: 100, label, startedAt: new Date().toISOString() };
      setJobs((items) => [cached, ...items.filter((item) => item.task_id !== taskId)].slice(0, 12));
      return cached;
    }
    controllers.current.get(taskId)?.abort();
    const controller = new AbortController();
    controllers.current.set(taskId, controller);
    const abortFromCaller = () => controller.abort();
    if (externalSignal?.aborted) controller.abort();
    else externalSignal?.addEventListener("abort", abortFromCaller, { once: true });
    const startedAt = new Date().toISOString();
    setJobs((items) => {
      if (items.some((item) => item.task_id === taskId)) {
        return items.map((item) => item.task_id === taskId ? { ...item, label } : item);
      }
      return [{ task_id: taskId, status: "PENDING", progress: 0, label, startedAt }, ...items].slice(0, 12);
    });
    try {
      return await pollJobUntilTerminal({
        taskId,
        label,
        signal: controller.signal,
        onStatus: (status) => {
          if (mounted.current) {
            setJobs((items) => items.map((item) => item.task_id === taskId ? { ...item, ...status } : item));
          }
        },
      });
    } catch (error) {
      if (controller.signal.aborted) throw new Error(`${label} was cancelled.`);
      if (mounted.current) {
        const failure = monitoringFailureStatus(error);
        setJobs((items) => items.map((item) => item.task_id === taskId && !isTerminalJobStatus(item.status) ? {
          ...item,
          ...failure,
        } : item));
      }
      throw error;
    } finally {
      externalSignal?.removeEventListener("abort", abortFromCaller);
      if (controllers.current.get(taskId) === controller) controllers.current.delete(taskId);
    }
  }, []);

  const cancel = useCallback(async (taskId: string, options: CancelJobOptions & { label?: string } = {}) => {
    const label = options.label || jobs.find((item) => item.task_id === taskId)?.label || "Background job";
    try {
      await cancelJob(taskId, options);
    } catch (error) {
      // The original polling may already have been aborted by the caller. A
      // terminal-state race (409) is not a cancellation failure; reattach so
      // the UI converges to the durable server state. For other failures we
      // still reattach before surfacing the error.
      void track(taskId, label).catch(() => undefined);
      if (axios.isAxiosError(error) && error.response?.status === 409) return;
      throw error;
    }
    const startedAt = new Date().toISOString();
    setJobs((items) => {
      const cancellationRequested: TrackedJob = {
        task_id: taskId,
        status: "CANCELLATION_REQUESTED",
        progress: 0,
        stage: "cancellation_requested",
        label,
        startedAt,
      };
      return items.some((item) => item.task_id === taskId)
        ? items.map((item) => item.task_id === taskId ? { ...item, status: "CANCELLATION_REQUESTED", stage: "cancellation_requested" } : item)
        : [cancellationRequested, ...items].slice(0, 12);
    });
    void track(taskId, label).catch(() => undefined);
  }, [jobs, track]);

  const reset = useCallback(() => {
    controllers.current.forEach((controller) => controller.abort());
    controllers.current.clear();
    setJobs([]);
  }, []);

  const resume = useCallback((taskId: string) => {
    const label = jobs.find((item) => item.task_id === taskId)?.label || "Background job";
    return track(taskId, label);
  }, [jobs, track]);

  return { jobs, track, cancel, resume, reset };
}
