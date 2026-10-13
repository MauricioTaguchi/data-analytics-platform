import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from "axios";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiClient, authenticate, fetchOperationCatalog, applyDatasetTransformation, saveQualityContract, startQualityEvaluation, pinDatasetSnapshot, cancelJob, createChart, createDashboard, defaultApiUrlForHostname, disconnectApi, downloadDataset, fetchDataset, hasActiveSession, onSessionExpired, startDatasetProfile, undoDatasetTransformation } from "./api-client";

const originalAdapter = apiClient.defaults.adapter;

function response(config: InternalAxiosRequestConfig, status: number, data: unknown): AxiosResponse {
  return { config, status, statusText: String(status), headers: {}, data };
}

beforeEach(() => {
  apiClient.defaults.adapter = originalAdapter;
});

afterEach(async () => {
  vi.useRealTimers();
  apiClient.defaults.adapter = async (config) => response(config, 204, null);
  await disconnectApi();
  apiClient.defaults.adapter = originalAdapter;
});

describe("API session client", () => {
  it.each([
    ['attachment; filename="processed-sales.parquet"', "processed-sales.parquet"],
    ["attachment; filename=processed-sales.parquet", "processed-sales.parquet"],
    ['attachment; filename="processed-sales;2026.parquet"', "processed-sales;2026.parquet"],
    ['attachment; filename="processed-vendas São Paulo.parquet"', "processed-vendas São Paulo.parquet"],
    ["attachment; filename*=UTF-8''processed-vendas%20S%C3%A3o%20Paulo.parquet", "processed-vendas São Paulo.parquet"],
    ['attachment; filename="fallback.parquet"; filename*=UTF-8\'pt-BR\'processed-vendas%20S%C3%A3o%20Paulo.parquet', "processed-vendas São Paulo.parquet"],
    ["attachment; filename*=ISO-8859-1''processed-caf%E9.parquet", "processed-café.parquet"],
    ['attachment; filename="fallback.parquet"; filename*=UTF-8\'\'%broken', "fallback.parquet"],
    ['attachment; filename="../../processed-sales.parquet"', "processed-sales.parquet"],
    ["attachment; filename*=UTF-8''%invalid", "processed-dataset.bin"],
    [undefined, "processed-dataset.bin"],
  ])("preserves the server export filename from %s", async (disposition, expectedFilename) => {
    const blob = new Blob(["PAR1"], { type: "application/octet-stream" });
    apiClient.defaults.adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
      expect(config.url).toBe("/datasets/7/export");
      expect(config.responseType).toBe("blob");
      return { ...response(config, 200, blob), headers: { "content-disposition": disposition } };
    });

    const result = await downloadDataset(7);
    expect(result.blob).toBe(blob);
    expect(result.filename).toBe(expectedFilename);
  });

  it("sends the observed revision when undoing and surfaces conflicts", async () => {
    const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
      expect(config.url).toBe("/datasets/7/transformations/undo");
      expect(JSON.parse(String(config.data))).toEqual({ expected_version: 8 });
      expect(config.headers.get("Idempotency-Key")).toBe("undo-revision-8");
      const conflict = response(config, 409, { detail: "Dataset version changed." });
      throw new AxiosError("Dataset version changed.", "ERR_BAD_REQUEST", config, undefined, conflict);
    });
    apiClient.defaults.adapter = adapter;
    await expect(undoDatasetTransformation(7, 8, "undo-revision-8")).rejects.toThrow("Dataset version changed.");
    expect(adapter).toHaveBeenCalledTimes(1);
  });

  it("replays an undo with the same key and revision after an ambiguous response", async () => {
    vi.useFakeTimers();
    const receipt = { event_id: "audit-event", previous_version: 8, result_version: 9, status: "undone" };
    let attempts = 0;
    apiClient.defaults.adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
      expect(config.headers.get("Idempotency-Key")).toBe("undo-revision-8");
      expect(JSON.parse(String(config.data))).toEqual({ expected_version: 8 });
      attempts += 1;
      if (attempts === 1) throw new AxiosError("Response lost", "ERR_NETWORK", config);
      return response(config, 200, receipt);
    });

    const result = undoDatasetTransformation(7, 8, "undo-revision-8");
    await vi.runAllTimersAsync();
    await expect(result).resolves.toEqual(receipt);
    expect(attempts).toBe(2);
  });

  it("limits transient undo retries without changing its identity", async () => {
    vi.useFakeTimers();
    const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
      expect(config.headers.get("Idempotency-Key")).toBe("undo-revision-8");
      expect(JSON.parse(String(config.data))).toEqual({ expected_version: 8 });
      const unavailable = response(config, 503, { detail: "Service unavailable" });
      throw new AxiosError("Service unavailable", "ERR_BAD_RESPONSE", config, undefined, unavailable);
    });
    apiClient.defaults.adapter = adapter;

    const assertion = expect(undoDatasetTransformation(7, 8, "undo-revision-8")).rejects.toThrow("Service unavailable");
    await vi.runAllTimersAsync();
    await assertion;
    expect(adapter).toHaveBeenCalledTimes(3);
  });

  it("uses the production API only for the explicitly trusted web host", () => {
    expect(defaultApiUrlForHostname("data-analytics-web.onrender.com"))
      .toBe("https://data-analytics-api.onrender.com/api/v1");
    expect(defaultApiUrlForHostname("DATA-ANALYTICS-WEB.ONRENDER.COM."))
      .toBe("https://data-analytics-api.onrender.com/api/v1");
    expect(defaultApiUrlForHostname("attackerdata-analytics-web.onrender.com"))
      .toBe("http://localhost:8000/api/v1");
    expect(defaultApiUrlForHostname("data-analytics-web.onrender.com.attacker.example"))
      .toBe("http://localhost:8000/api/v1");
  });

  it("forwards cancellation to dashboard and chart mutations", async () => {
    const controller = new AbortController();
    const observedSignals: Array<AbortSignal | undefined> = [];
    apiClient.defaults.adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
      observedSignals.push(config.signal as AbortSignal | undefined);
      if (config.url === "/dashboards") {
        return response(config, 201, { id: 3, project_id: 2, name: "Executive dashboard" });
      }
      if (config.url === "/dashboards/3/charts") {
        return response(config, 201, { id: 5, dashboard_id: 3, title: "Revenue", chart_type: "bar" });
      }
      throw new Error(`Unexpected request: ${config.method} ${config.url}`);
    });

    await createDashboard(2, "Executive dashboard", controller.signal);
    await createChart(3, {
      dataset_id: 7,
      title: "Revenue",
      chart_type: "bar",
      x_column: "category",
      y_column: "total",
      aggregation: "sum",
    }, controller.signal);

    expect(observedSignals).toEqual([controller.signal, controller.signal]);
  });

  it("rotates an expired access token and retries the original request once", async () => {
    let datasetAttempts = 0;
    const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
      if (config.url === "/auth/login") {
        return response(config, 200, { access_token: "access-1", refresh_token: "refresh-1" });
      }
      if (config.url === "/auth/refresh") {
        expect(JSON.parse(String(config.data))).toEqual({ refresh_token: "refresh-1" });
        return response(config, 200, { access_token: "access-2", refresh_token: "refresh-2" });
      }
      if (config.url === "/datasets/7") {
        datasetAttempts += 1;
        if (datasetAttempts === 1) {
          const unauthorized = response(config, 401, { detail: "Expired token" });
          throw new AxiosError("Request failed with status code 401", "ERR_BAD_REQUEST", config, undefined, unauthorized);
        }
        expect(config.headers?.Authorization).toBe("Bearer access-2");
        return response(config, 200, {
          id: 7,
          original_filename: "orders.csv",
          status: "ready",
          row_count: 10,
          column_count: 4,
          version: 1,
        });
      }
      throw new Error(`Unexpected request: ${config.method} ${config.url}`);
    });
    apiClient.defaults.adapter = adapter;

    await authenticate({ intent: "login", name: "", email: "owner@example.com", password: "safe-password-123" });
    const dataset = await fetchDataset(7);

    expect(dataset.id).toBe(7);
    expect(hasActiveSession()).toBe(true);
    expect(datasetAttempts).toBe(2);
    expect(adapter.mock.calls.filter(([config]) => config.url === "/auth/refresh")).toHaveLength(1);
  });

  it("revokes a refresh token created after logout wins a session race", async () => {
    let resolveRefresh!: (value: AxiosResponse) => void;
    let refreshStarted = false;
    const revokedTokens: string[] = [];
    const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
      if (config.url === "/auth/login") {
        return response(config, 200, { access_token: "access-1", refresh_token: "refresh-1" });
      }
      if (config.url === "/auth/refresh") {
        refreshStarted = true;
        return new Promise<AxiosResponse>((resolve) => { resolveRefresh = resolve; });
      }
      if (config.url === "/auth/logout") {
        revokedTokens.push(JSON.parse(String(config.data)).refresh_token);
        return response(config, 204, null);
      }
      if (config.url === "/datasets/7") {
        const unauthorized = response(config, 401, { detail: "Expired token" });
        throw new AxiosError("Request failed with status code 401", "ERR_BAD_REQUEST", config, undefined, unauthorized);
      }
      throw new Error(`Unexpected request: ${config.method} ${config.url}`);
    });
    apiClient.defaults.adapter = adapter;

    await authenticate({ intent: "login", name: "", email: "owner@example.com", password: "safe-password-123" });
    const pendingDataset = fetchDataset(7);
    await vi.waitFor(() => expect(refreshStarted).toBe(true));
    await disconnectApi();
    resolveRefresh(response(
      { headers: {} } as InternalAxiosRequestConfig,
      200,
      { access_token: "access-2", refresh_token: "refresh-2" },
    ));

    await expect(pendingDataset).rejects.toThrow("session was closed");
    expect(revokedTokens).toEqual(["refresh-1", "refresh-2"]);
    expect(hasActiveSession()).toBe(false);
  });

  it("keeps a newly authenticated session independent from an older refresh in flight", async () => {
    let loginCount = 0;
    let resolveOldRefresh!: (value: AxiosResponse) => void;
    let oldRefreshStarted = false;
    const revokedTokens: string[] = [];
    const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
      if (config.url === "/auth/login") {
        loginCount += 1;
        return response(config, 200, loginCount === 1
          ? { access_token: "old-access", refresh_token: "old-refresh" }
          : { access_token: "new-access-1", refresh_token: "new-refresh-1" });
      }
      if (config.url === "/auth/refresh") {
        const token = JSON.parse(String(config.data)).refresh_token;
        if (token === "old-refresh") {
          oldRefreshStarted = true;
          return new Promise<AxiosResponse>((resolve) => { resolveOldRefresh = resolve; });
        }
        expect(token).toBe("new-refresh-1");
        return response(config, 200, { access_token: "new-access-2", refresh_token: "new-refresh-2" });
      }
      if (config.url === "/auth/logout") {
        revokedTokens.push(JSON.parse(String(config.data)).refresh_token);
        return response(config, 204, null);
      }
      if (config.url === "/datasets/7") {
        if (config.headers?.Authorization !== "Bearer new-access-2") {
          const unauthorized = response(config, 401, { detail: "Expired token" });
          throw new AxiosError("Request failed with status code 401", "ERR_BAD_REQUEST", config, undefined, unauthorized);
        }
        return response(config, 200, {
          id: 7,
          original_filename: "orders.csv",
          status: "ready",
          row_count: 10,
          column_count: 4,
          version: 1,
        });
      }
      throw new Error(`Unexpected request: ${config.method} ${config.url}`);
    });
    apiClient.defaults.adapter = adapter;

    await authenticate({ intent: "login", name: "", email: "old@example.com", password: "safe-password-123" });
    const oldRequest = fetchDataset(7);
    await vi.waitFor(() => expect(oldRefreshStarted).toBe(true));

    await authenticate({ intent: "login", name: "", email: "new@example.com", password: "safe-password-123" });
    const currentDataset = await fetchDataset(7);
    resolveOldRefresh(response(
      { headers: {} } as InternalAxiosRequestConfig,
      200,
      { access_token: "old-access-2", refresh_token: "old-refresh-2" },
    ));

    await expect(oldRequest).rejects.toThrow("session was closed");
    expect(currentDataset.id).toBe(7);
    expect(revokedTokens).toContain("old-refresh-2");
    expect(hasActiveSession()).toBe(true);
  });

  it("expires the live session only after a definitive refresh rejection", async () => {
    const onExpired = vi.fn();
    const unsubscribe = onSessionExpired(onExpired);
    const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
      if (config.url === "/auth/login") {
        return response(config, 200, { access_token: "expired-access", refresh_token: "expired-refresh" });
      }
      if (config.url === "/datasets/7") {
        const unauthorized = response(config, 401, { detail: "Expired token" });
        throw new AxiosError("Request failed with status code 401", "ERR_BAD_REQUEST", config, undefined, unauthorized);
      }
      if (config.url === "/auth/refresh") {
        const rejected = response(config, 401, { detail: "Refresh token expired" });
        throw new AxiosError("Request failed with status code 401", "ERR_BAD_REQUEST", config, undefined, rejected);
      }
      throw new Error(`Unexpected request: ${config.method} ${config.url}`);
    });
    apiClient.defaults.adapter = adapter;

    try {
      await authenticate({ intent: "login", name: "", email: "owner@example.com", password: "safe-password-123" });
      await expect(fetchDataset(7)).rejects.toThrow("Request failed with status code 401");

      expect(hasActiveSession()).toBe(false);
      expect(onExpired).toHaveBeenCalledTimes(1);
    } finally {
      unsubscribe();
    }
  });

  it("keeps refresh credentials after a transient refresh transport failure", async () => {
    const onExpired = vi.fn();
    const unsubscribe = onSessionExpired(onExpired);
    const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
      if (config.url === "/auth/login") {
        return response(config, 200, { access_token: "access-1", refresh_token: "refresh-1" });
      }
      if (config.url === "/datasets/7") {
        const unauthorized = response(config, 401, { detail: "Expired token" });
        throw new AxiosError("Request failed with status code 401", "ERR_BAD_REQUEST", config, undefined, unauthorized);
      }
      if (config.url === "/auth/refresh") {
        throw new AxiosError("Network unavailable", "ERR_NETWORK", config);
      }
      throw new Error(`Unexpected request: ${config.method} ${config.url}`);
    });
    apiClient.defaults.adapter = adapter;

    try {
      await authenticate({ intent: "login", name: "", email: "owner@example.com", password: "safe-password-123" });
      await expect(fetchDataset(7)).rejects.toThrow("Network unavailable");

      expect(hasActiveSession()).toBe(true);
      expect(onExpired).not.toHaveBeenCalled();
    } finally {
      unsubscribe();
    }
  });

  it("sends the caller-generated task identifier before a job is created", async () => {
    const taskId = "72915ee6-c52c-4c06-a071-494d06f26788";
    apiClient.defaults.adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
      expect(config.url).toBe("/datasets/7/profile");
      expect(config.headers.get("X-Task-ID")).toBe(taskId);
      return response(config, 202, { task_id: taskId, status: "PENDING", progress: 0 });
    });

    await expect(startDatasetProfile(7, taskId)).resolves.toMatchObject({ task_id: taskId });
  });

  it("retries cancellation while a caller-generated job record is still being created", async () => {
    const taskId = "e38a72a4-7bbc-4d41-9d15-398a2cc4e765";
    let attempts = 0;
    apiClient.defaults.adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
      expect(config.url).toBe(`/datasets/jobs/${taskId}`);
      attempts += 1;
      if (attempts === 1) {
        const missing = response(config, 404, { detail: "Job not created yet" });
        throw new AxiosError("Request failed with status code 404", "ERR_BAD_REQUEST", config, undefined, missing);
      }
      return response(config, 202, null);
    });

    await cancelJob(taskId, { retryNotFound: true });

    expect(attempts).toBe(2);
  });
});


describe("governed dataset APIs", () => {
  it("rejects unavailable or empty catalogs rather than inventing local live capabilities", async () => {
    apiClient.defaults.adapter = async (config) => response(config, 200, []);
    await expect(fetchOperationCatalog()).rejects.toThrow("invalid operation catalog");
  });
  it("submits explicitly chosen engines with typed scalar maps", async () => {
    apiClient.defaults.adapter = async (config) => {
      expect(config.url).toBe("/datasets/7/transform");
      expect(JSON.parse(String(config.data))).toEqual({ operation: "fill_nulls", parameters: { values: { id: "001", active: false } }, expected_version: 4, engine: "pandas" });
      expect(config.headers.get("Idempotency-Key")).toBe("job-key");
      return response(config, 202, { task_id: "job-key" });
    };
    await applyDatasetTransformation(7, "fill_nulls", { values: { id: "001", active: false } }, 4, "job-key", "pandas");
  });
  it("uses contract revision guards and durable evaluation identities", async () => {
    apiClient.defaults.adapter = async (config) => {
      const data = JSON.parse(String(config.data));
      if (config.url?.endsWith("/contracts")) {
        expect(data).toEqual({ name: "Orders", rules: [{ id: "required", kind: "not_null", column: "id" }], expected_contract_version: 2 });
        return response(config, 201, { id: 9, version: 3 });
      }
      expect(config.url).toBe("/datasets/7/quality/evaluations");
      expect(data).toEqual({ contract_id: 9, expected_version: 4 });
      expect(config.headers.get("Idempotency-Key")).toBe("quality-job");
      expect(config.headers.get("X-Task-ID")).toBe("quality-job");
      return response(config, 202, { task_id: "quality-job" });
    };
    await saveQualityContract(7, "Orders", [{ id: "required", kind: "not_null", column: "id" }], 2);
    await startQualityEvaluation(7, 9, 4, "quality-job");
  });
  it("pins only the requested snapshot and leaves retention validation to the API", async () => {
    apiClient.defaults.adapter = async (config) => {
      expect(config.method).toBe("patch");
      expect(config.url).toBe("/datasets/7/snapshots/snapshot-id");
      expect(JSON.parse(String(config.data))).toEqual({ pinned: true });
      return response(config, 200, { id: "snapshot-id", pinned: true });
    };
    expect(await pinDatasetSnapshot(7, "snapshot-id", true)).toEqual({ id: "snapshot-id", pinned: true });
  });
});
