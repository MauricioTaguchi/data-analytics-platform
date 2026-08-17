import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from "axios";
import { describe, expect, it, vi } from "vitest";

import { isResumableJobStatus, JobPollingTimeoutError, monitoringFailureStatus, pollJobUntilTerminal } from "./useProfilingJob";

describe("job polling", () => {
  it("allows monitoring to resume after a timeout or a non-retryable status error", () => {
    expect(isResumableJobStatus("TIMEOUT")).toBe(true);
    expect(isResumableJobStatus("MONITORING_ERROR")).toBe(true);
    expect(isResumableJobStatus("FAILURE")).toBe(false);
    expect(monitoringFailureStatus(new Error("Job status is unavailable."))).toEqual({
      status: "MONITORING_ERROR",
      stage: "monitoring_error",
      error_message: "Job status is unavailable.",
    });
  });

  it("recognizes the durable backend cancellation state as terminal", async () => {
    const onStatus = vi.fn();

    await expect(pollJobUntilTerminal({
      taskId: "job-cancelled",
      label: "Dataset import",
      signal: new AbortController().signal,
      fetchStatus: vi.fn().mockResolvedValue({
        task_id: "job-cancelled",
        status: "CANCELLED",
        progress: 25,
      }),
      onStatus,
      wait: async () => undefined,
      now: () => 0,
    })).rejects.toThrow("finished with status CANCELLED");

    expect(onStatus).toHaveBeenCalledTimes(1);
  });

  it("surfaces the durable worker error instead of replacing it", async () => {
    await expect(pollJobUntilTerminal({
      taskId: "job-failed",
      label: "Dataset import",
      signal: new AbortController().signal,
      fetchStatus: vi.fn().mockResolvedValue({
        task_id: "job-failed",
        status: "FAILURE",
        progress: 40,
        error_message: "The source file is malformed.",
      }),
      wait: async () => undefined,
      now: () => 0,
    })).rejects.toThrow("The source file is malformed.");
  });

  it("polls until success and emits normalized progress", async () => {
    const fetchStatus = vi.fn()
      .mockResolvedValueOnce({ task_id: "job-1", status: "PENDING", progress: -10 })
      .mockResolvedValueOnce({ task_id: "job-1", status: "SUCCESS", progress: 110 });
    const onStatus = vi.fn();

    const result = await pollJobUntilTerminal({
      taskId: "job-1",
      label: "Dataset import",
      signal: new AbortController().signal,
      fetchStatus,
      onStatus,
      wait: async () => undefined,
      now: () => 0,
    });

    expect(result).toMatchObject({ status: "SUCCESS", progress: 100 });
    expect(onStatus).toHaveBeenNthCalledWith(1, expect.objectContaining({ progress: 0 }));
    expect(onStatus).toHaveBeenNthCalledWith(2, expect.objectContaining({ progress: 100 }));
  });

  it("recovers from transient monitoring failures without abandoning the job", async () => {
    const config = { headers: {} } as InternalAxiosRequestConfig;
    const unavailable = {
      config,
      status: 502,
      statusText: "Bad Gateway",
      headers: {},
      data: { detail: "Temporary upstream failure" },
    } as AxiosResponse;
    const fetchStatus = vi.fn()
      .mockRejectedValueOnce(new AxiosError("Temporary failure", "ERR_BAD_RESPONSE", config, undefined, unavailable))
      .mockResolvedValueOnce({ task_id: "job-recovered", status: "SUCCESS", progress: 100 });
    const wait = vi.fn().mockResolvedValue(undefined);

    const result = await pollJobUntilTerminal({
      taskId: "job-recovered",
      label: "Dataset import",
      signal: new AbortController().signal,
      fetchStatus,
      wait,
      now: () => 0,
    });

    expect(result.status).toBe("SUCCESS");
    expect(fetchStatus).toHaveBeenCalledTimes(2);
    expect(wait).toHaveBeenCalledWith(expect.any(AbortSignal), 1_500);
  });

  it("fails with an actionable timeout when the monitoring deadline expires", async () => {
    const now = vi.fn().mockReturnValueOnce(0).mockReturnValue(120_000);

    await expect(pollJobUntilTerminal({
      taskId: "job-2",
      label: "Dataset profiling",
      signal: new AbortController().signal,
      fetchStatus: vi.fn(),
      wait: async () => undefined,
      now,
      timeoutMs: 120_000,
    })).rejects.toBeInstanceOf(JobPollingTimeoutError);
  });
});
