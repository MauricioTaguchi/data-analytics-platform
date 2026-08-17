import axios, { type InternalAxiosRequestConfig } from "axios";

import type { DataOperation, DataRow } from "./data-utils";


const LOCAL_API_URL = "http://localhost:8000/api/v1";
const PRODUCTION_API_URL = "https://data-analytics-api.onrender.com/api/v1";
const PRODUCTION_WEB_HOST = "data-analytics-web.onrender.com";

export function defaultApiUrlForHostname(hostname: string): string {
  const normalizedHostname = hostname.trim().toLowerCase().replace(/\.$/, "");
  return normalizedHostname === PRODUCTION_WEB_HOST ? PRODUCTION_API_URL : LOCAL_API_URL;
}

const hostname = typeof window === "undefined" ? "" : window.location.hostname;
const deployedApi = defaultApiUrlForHostname(hostname);

export const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_URL || deployedApi,
  timeout: 20_000,
});

let accessToken: string | null = null;
let refreshToken: string | null = null;
let refreshState: { token: string; promise: Promise<string> } | null = null;
const sessionExpiredListeners = new Set<() => void>();

type RetryableRequest = InternalAxiosRequestConfig & { _retry?: boolean };

export type AuthenticationInput = {
  intent: "login" | "register";
  name: string;
  email: string;
  password: string;
};

type TokenResponse = {
  access_token: string;
  refresh_token: string;
};

export type Project = {
  id: number;
  name: string;
};

export type Dataset = {
  id: number;
  original_filename: string;
  status: string;
  row_count: number | null;
  column_count: number | null;
  version: number;
};

export type DatasetPreview = {
  columns: string[];
  rows: DataRow[];
  total_rows: number;
  total_columns: number;
  columns_truncated: number;
};

export type JobStatus = {
  task_id: string;
  status: string;
  progress: number;
  stage?: string | null;
  error_message?: string | null;
  result?: Record<string, unknown> | null;
};

export type TransformationResult = {
  id: number;
  operation: DataOperation;
  parameters: Record<string, unknown>;
  before_rows: number;
  after_rows: number;
  before_columns: number;
  after_columns: number;
  status: string;
  task_id: string | null;
  expected_version: number;
  undone_at: string | null;
  created_at: string;
};

export type TransformationPreview = {
  before: { rows: number; columns: number; missing_cells: number };
  after: { rows: number; columns: number; missing_cells: number };
  rows: DataRow[];
};

export type Dashboard = {
  id: number;
  project_id: number;
  name: string;
  description: string | null;
  layout_json: Record<string, unknown>;
  created_at: string;
};

export type ChartType = "bar" | "line" | "pie" | "histogram" | "scatter" | "table" | "kpi";
export type Aggregation = "sum" | "mean" | "count" | "min" | "max";

export type Chart = {
  id: number;
  dashboard_id: number;
  dataset_id: number;
  title: string;
  chart_type: ChartType;
  x_column: string | null;
  y_column: string | null;
  aggregation: Aggregation | null;
  filters_json: Record<string, unknown>;
  created_at: string;
};

export type ChartData = {
  labels: Array<string | number>;
  values: Array<string | number | null>;
  rows: DataRow[];
};

export type Report = {
  id: number;
  project_id: number;
  dataset_id: number;
  status: string;
  task_id: string | null;
  error_message: string | null;
  created_at: string;
};


function storeTokens(tokens: TokenResponse) {
  accessToken = tokens.access_token;
  refreshToken = tokens.refresh_token;
}

function clearTokens() {
  accessToken = null;
  refreshToken = null;
}

function expireSession() {
  clearTokens();
  sessionExpiredListeners.forEach((listener) => listener());
}

function isDefinitiveRefreshFailure(error: unknown) {
  return axios.isAxiosError(error) && [400, 401, 403].includes(error.response?.status || 0);
}

async function rotateSession() {
  if (!refreshToken) throw new Error("The session cannot be renewed.");
  if (!refreshState || refreshState.token !== refreshToken) {
    const tokenBeingRotated = refreshToken;
    let promise!: Promise<string>;
    promise = apiClient
      .post<TokenResponse>("/auth/refresh", { refresh_token: tokenBeingRotated })
      .then(async (response) => {
        if (refreshToken !== tokenBeingRotated) {
          try {
            await apiClient.post("/auth/logout", { refresh_token: response.data.refresh_token });
          } catch {
            // The local session remains closed even if best-effort revocation is unavailable.
          }
          throw new Error("The session was closed while it was being renewed.");
        }
        storeTokens(response.data);
        return response.data.access_token;
      })
      .catch((error) => {
        if (refreshToken === tokenBeingRotated && isDefinitiveRefreshFailure(error)) expireSession();
        throw error;
      })
      .finally(() => {
        if (refreshState?.promise === promise) refreshState = null;
      });
    refreshState = { token: tokenBeingRotated, promise };
  }
  return refreshState.promise;
}

apiClient.interceptors.request.use((config) => {
  if (accessToken) config.headers.Authorization = `Bearer ${accessToken}`;
  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  async (error) => {
    const request = error.config as RetryableRequest | undefined;
    const isAuthenticationRequest = String(request?.url || "").includes("/auth/");
    if (error.response?.status === 401 && request && !request._retry && refreshToken && !isAuthenticationRequest) {
      request._retry = true;
      const renewedAccessToken = await rotateSession();
      request.headers.Authorization = `Bearer ${renewedAccessToken}`;
      return apiClient.request(request);
    }
    throw error;
  },
);


export function apiBaseUrl() {
  return String(apiClient.defaults.baseURL || "");
}

export function hasActiveSession() {
  return Boolean(accessToken && refreshToken);
}

export function onSessionExpired(listener: () => void) {
  sessionExpiredListeners.add(listener);
  return () => { sessionExpiredListeners.delete(listener); };
}

export async function checkApiHealth() {
  const healthUrl = apiBaseUrl().replace(/\/api\/v1\/?$/, "/health/ready");
  const response = await axios.get<{ status: string }>(healthUrl, { timeout: 5_000 });
  return response.data.status;
}

export async function authenticate(input: AuthenticationInput) {
  const payload = input.intent === "register"
    ? { name: input.name, email: input.email, password: input.password }
    : { email: input.email, password: input.password };
  const response = await apiClient.post<TokenResponse>(`/auth/${input.intent}`, payload);
  storeTokens(response.data);
  return response.data;
}

export async function disconnectApi() {
  const tokenToRevoke = refreshToken;
  clearTokens();
  if (!tokenToRevoke) return;
  try {
    await apiClient.post("/auth/logout", { refresh_token: tokenToRevoke });
  } catch {
    // Local credentials are cleared even when the remote service is unavailable.
  }
}

export async function getOrCreatePortfolioProject() {
  const projects = await apiClient.get<Project[]>("/projects");
  if (projects.data.length > 0) return projects.data[0];
  const created = await apiClient.post<Project>("/projects", {
    name: "Portfolio workspace",
    description: "Workspace created from the DataFlow web application.",
  });
  return created.data;
}

type UploadOptions = {
  taskId: string;
  signal?: AbortSignal;
  onProgress?: (progress: number) => void;
};

export async function uploadDataset(projectId: number, file: File, options: UploadOptions) {
  const form = new FormData();
  form.append("file", file);
  const response = await apiClient.post<{ dataset_id: number; task_id: string; status: string }>(
    `/datasets/project/${projectId}`,
    form,
    {
      headers: { "X-Task-ID": options.taskId },
      signal: options.signal,
      timeout: 120_000,
      onUploadProgress: (event) => {
        const total = event.total || file.size;
        if (total > 0) options.onProgress?.(Math.min(99, Math.round((event.loaded / total) * 100)));
      },
    },
  );
  options.onProgress?.(100);
  return response.data;
}

export async function fetchDataset(datasetId: number, signal?: AbortSignal) {
  const response = await apiClient.get<Dataset>(`/datasets/${datasetId}`, { signal });
  return response.data;
}

export async function fetchDatasetPreview(datasetId: number, signal?: AbortSignal) {
  const response = await apiClient.get<DatasetPreview>(`/datasets/${datasetId}/preview`, {
    params: { page: 1, page_size: 100 },
    signal,
  });
  return response.data;
}

export async function startDatasetProfile(datasetId: number, taskId: string, signal?: AbortSignal) {
  const response = await apiClient.post<JobStatus>(`/datasets/${datasetId}/profile`, undefined, {
    headers: { "X-Task-ID": taskId },
    signal,
  });
  return response.data;
}

export async function fetchJobStatus(taskId: string, signal?: AbortSignal) {
  const response = await apiClient.get<JobStatus>(`/datasets/jobs/${taskId}`, { signal });
  return response.data;
}

export type CancelJobOptions = { retryNotFound?: boolean };

function wait(delayMs: number, signal?: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Request cancelled", "AbortError"));
      return;
    }
    const onAbort = () => {
      globalThis.clearTimeout(timeoutId);
      reject(new DOMException("Request cancelled", "AbortError"));
    };
    const timeoutId = globalThis.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, delayMs);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export async function cancelJob(taskId: string, options: CancelJobOptions = {}) {
  const delays = options.retryNotFound ? [0, 150, 350, 750, 1_250] : [0];
  let lastError: unknown;
  for (const delayMs of delays) {
    if (delayMs) await wait(delayMs);
    try {
      await apiClient.delete(`/datasets/jobs/${taskId}`);
      return;
    } catch (error) {
      lastError = error;
      if (!axios.isAxiosError(error) || error.response?.status !== 404) throw error;
    }
  }
  throw lastError;
}

export function isAmbiguousJobCreationError(error: unknown) {
  if (!axios.isAxiosError(error) || isRequestCancelled(error)) return false;
  const status = error.response?.status;
  return !status || [408, 425, 429].includes(status) || status >= 500;
}

export async function reconcileCreatedJob(taskId: string, signal?: AbortSignal) {
  const delays = [0, 200, 500, 1_000];
  for (const delayMs of delays) {
    if (delayMs) await wait(delayMs, signal);
    try {
      return await fetchJobStatus(taskId, signal);
    } catch (error) {
      if (isRequestCancelled(error)) throw error;
      if (!axios.isAxiosError(error)) return null;
      const status = error.response?.status;
      const retryable = !status || status === 404 || [408, 425, 429].includes(status) || status >= 500;
      if (!retryable) return null;
    }
  }
  return null;
}

export async function previewDatasetTransformation(
  datasetId: number,
  operation: DataOperation,
  parameters: object,
  expectedVersion: number,
  taskId: string,
) {
  const response = await apiClient.post<JobStatus>(`/datasets/${datasetId}/transform/preview`, {
    operation,
    parameters,
    expected_version: expectedVersion,
  }, { headers: { "X-Task-ID": taskId } });
  return response.data;
}

export async function applyDatasetTransformation(
  datasetId: number,
  operation: DataOperation,
  parameters: object,
  expectedVersion: number,
  taskId: string,
) {
  const response = await apiClient.post<{
    transformation_id: number;
    task_id: string;
    status: string;
    reused: boolean;
  }>(
    `/datasets/${datasetId}/transform`,
    { operation, parameters, expected_version: expectedVersion },
    { headers: { "Idempotency-Key": taskId, "X-Task-ID": taskId } },
  );
  return response.data;
}

export async function fetchTransformationHistory(datasetId: number, signal?: AbortSignal) {
  const response = await apiClient.get<TransformationResult[]>(`/datasets/${datasetId}/transformations`, { signal });
  return response.data;
}

export async function undoDatasetTransformation(datasetId: number) {
  const response = await apiClient.post<TransformationResult>(`/datasets/${datasetId}/transformations/undo`);
  return response.data;
}

export async function downloadDataset(datasetId: number) {
  const response = await apiClient.get<Blob>(`/datasets/${datasetId}/export`, { responseType: "blob" });
  return response.data;
}

export async function listDashboards(projectId: number, signal?: AbortSignal) {
  const response = await apiClient.get<Dashboard[]>(`/dashboards/project/${projectId}`, { signal });
  return response.data;
}

export async function createDashboard(projectId: number, name: string, signal?: AbortSignal) {
  const response = await apiClient.post<Dashboard>("/dashboards", {
    project_id: projectId,
    name,
    description: "Interactive dashboard created in DataFlow.",
    layout_json: {},
  }, { signal });
  return response.data;
}

export async function listCharts(dashboardId: number, signal?: AbortSignal) {
  const response = await apiClient.get<Chart[]>(`/dashboards/${dashboardId}/charts`, { signal });
  return response.data;
}

export async function createChart(
  dashboardId: number,
  input: Omit<Chart, "id" | "dashboard_id" | "created_at" | "filters_json">,
  signal?: AbortSignal,
) {
  const response = await apiClient.post<Chart>(`/dashboards/${dashboardId}/charts`, {
    ...input,
    filters_json: {},
  }, { signal });
  return response.data;
}

export async function fetchChartData(chartId: number, signal?: AbortSignal) {
  const response = await apiClient.get<ChartData>(`/dashboards/charts/${chartId}/data`, { signal });
  return response.data;
}

export async function listReports(projectId: number, signal?: AbortSignal) {
  const response = await apiClient.get<Report[]>(`/reports/project/${projectId}`, { signal });
  return response.data;
}

export async function createReport(projectId: number, datasetId: number, taskId: string) {
  const response = await apiClient.post<{ report_id: number; task_id: string; status: string }>(
    `/reports/project/${projectId}/dataset/${datasetId}`,
    undefined,
    { headers: { "X-Task-ID": taskId } },
  );
  return response.data;
}

export async function fetchReportStatus(reportId: number) {
  const response = await apiClient.get<{ report_id: number; status: string; download_url: string | null }>(
    `/reports/${reportId}`,
  );
  return response.data;
}

export async function downloadReport(reportId: number) {
  const response = await apiClient.get<Blob>(`/reports/${reportId}/download`, { responseType: "blob" });
  return response.data;
}

export async function deleteReport(reportId: number) {
  await apiClient.delete(`/reports/${reportId}`);
}

export function describeApiError(error: unknown) {
  if (isRequestCancelled(error)) return "The operation was cancelled.";
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string") return detail;
    if (error.code === "ECONNABORTED") return "The API took too long to respond.";
  }
  return error instanceof Error ? error.message : "The operation could not be completed.";
}

export function isRequestCancelled(error: unknown) {
  return axios.isCancel(error)
    || (axios.isAxiosError(error) && error.code === "ERR_CANCELED")
    || (error instanceof DOMException && error.name === "AbortError");
}
