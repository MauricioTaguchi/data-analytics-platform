import axios, { type InternalAxiosRequestConfig } from "axios";

import type { DataOperation, DataRow } from "./data-utils";


const deployedApi = window.location.hostname.endsWith("onrender.com")
  ? "https://data-analytics-api.onrender.com/api/v1"
  : "http://localhost:8000/api/v1";

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || deployedApi,
  timeout: 20_000,
});

let accessToken: string | null = null;
let refreshToken: string | null = null;
let refreshPromise: Promise<string> | null = null;

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
};

export type JobStatus = {
  task_id: string;
  status: string;
  progress: number;
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
  values: Array<string | number>;
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

async function rotateSession() {
  if (!refreshToken) throw new Error("The session cannot be renewed.");
  if (!refreshPromise) {
    refreshPromise = api
      .post<TokenResponse>("/auth/refresh", { refresh_token: refreshToken })
      .then((response) => {
        storeTokens(response.data);
        return response.data.access_token;
      })
      .catch((error) => {
        clearTokens();
        throw error;
      })
      .finally(() => {
        refreshPromise = null;
      });
  }
  return refreshPromise;
}

api.interceptors.request.use((config) => {
  if (accessToken) config.headers.Authorization = `Bearer ${accessToken}`;
  return config;
});

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const request = error.config as RetryableRequest | undefined;
    const isAuthenticationRequest = String(request?.url || "").includes("/auth/");
    if (error.response?.status === 401 && request && !request._retry && refreshToken && !isAuthenticationRequest) {
      request._retry = true;
      const renewedAccessToken = await rotateSession();
      request.headers.Authorization = `Bearer ${renewedAccessToken}`;
      return api.request(request);
    }
    throw error;
  },
);


export function apiBaseUrl() {
  return String(api.defaults.baseURL || "");
}

export function hasActiveSession() {
  return Boolean(accessToken && refreshToken);
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
  const response = await api.post<TokenResponse>(`/auth/${input.intent}`, payload);
  storeTokens(response.data);
  return response.data;
}

export async function disconnectApi() {
  const tokenToRevoke = refreshToken;
  clearTokens();
  if (!tokenToRevoke) return;
  try {
    await api.post("/auth/logout", { refresh_token: tokenToRevoke });
  } catch {
    // Local credentials are cleared even when the remote service is unavailable.
  }
}

export async function getOrCreatePortfolioProject() {
  const projects = await api.get<Project[]>("/projects");
  if (projects.data.length > 0) return projects.data[0];
  const created = await api.post<Project>("/projects", {
    name: "Portfolio workspace",
    description: "Workspace created from the DataFlow web application.",
  });
  return created.data;
}

export async function uploadDataset(projectId: number, file: File) {
  const form = new FormData();
  form.append("file", file);
  const response = await api.post<{ dataset_id: number; task_id: string; status: string }>(
    `/datasets/project/${projectId}`,
    form,
  );
  return response.data;
}

export async function fetchDataset(datasetId: number) {
  const response = await api.get<Dataset>(`/datasets/${datasetId}`);
  return response.data;
}

export async function fetchDatasetPreview(datasetId: number) {
  const response = await api.get<DatasetPreview>(`/datasets/${datasetId}/preview`, {
    params: { page: 1, page_size: 100 },
  });
  return response.data;
}

export async function startDatasetProfile(datasetId: number) {
  const response = await api.post<JobStatus>(`/datasets/${datasetId}/profile`);
  return response.data;
}

export async function fetchJobStatus(taskId: string) {
  const response = await api.get<JobStatus>(`/datasets/jobs/${taskId}`);
  return response.data;
}

export async function cancelJob(taskId: string) {
  await api.delete(`/datasets/jobs/${taskId}`);
}

export async function previewDatasetTransformation(
  datasetId: number,
  operation: DataOperation,
  parameters: object,
  expectedVersion: number,
) {
  const response = await api.post<JobStatus>(`/datasets/${datasetId}/transform/preview`, {
    operation,
    parameters,
    expected_version: expectedVersion,
  });
  return response.data;
}

export async function applyDatasetTransformation(
  datasetId: number,
  operation: DataOperation,
  parameters: object,
  expectedVersion: number,
) {
  const response = await api.post<{
    transformation_id: number;
    task_id: string;
    status: string;
    reused: boolean;
  }>(
    `/datasets/${datasetId}/transform`,
    { operation, parameters, expected_version: expectedVersion },
    { headers: { "Idempotency-Key": crypto.randomUUID() } },
  );
  return response.data;
}

export async function fetchTransformationHistory(datasetId: number) {
  const response = await api.get<TransformationResult[]>(`/datasets/${datasetId}/transformations`);
  return response.data;
}

export async function undoDatasetTransformation(datasetId: number) {
  const response = await api.post<TransformationResult>(`/datasets/${datasetId}/transformations/undo`);
  return response.data;
}

export async function downloadDataset(datasetId: number) {
  const response = await api.get<Blob>(`/datasets/${datasetId}/export`, { responseType: "blob" });
  return response.data;
}

export async function listDashboards(projectId: number) {
  const response = await api.get<Dashboard[]>(`/dashboards/project/${projectId}`);
  return response.data;
}

export async function createDashboard(projectId: number, name: string) {
  const response = await api.post<Dashboard>("/dashboards", {
    project_id: projectId,
    name,
    description: "Interactive dashboard created in DataFlow.",
    layout_json: {},
  });
  return response.data;
}

export async function listCharts(dashboardId: number) {
  const response = await api.get<Chart[]>(`/dashboards/${dashboardId}/charts`);
  return response.data;
}

export async function createChart(
  dashboardId: number,
  input: Omit<Chart, "id" | "dashboard_id" | "created_at" | "filters_json">,
) {
  const response = await api.post<Chart>(`/dashboards/${dashboardId}/charts`, {
    ...input,
    filters_json: {},
  });
  return response.data;
}

export async function fetchChartData(chartId: number) {
  const response = await api.get<ChartData>(`/dashboards/charts/${chartId}/data`);
  return response.data;
}

export async function listReports(projectId: number) {
  const response = await api.get<Report[]>(`/reports/project/${projectId}`);
  return response.data;
}

export async function createReport(projectId: number, datasetId: number) {
  const response = await api.post<{ report_id: number; task_id: string; status: string }>(
    `/reports/project/${projectId}/dataset/${datasetId}`,
  );
  return response.data;
}

export async function fetchReportStatus(reportId: number) {
  const response = await api.get<{ report_id: number; status: string; download_url: string | null }>(
    `/reports/${reportId}`,
  );
  return response.data;
}

export async function downloadReport(reportId: number) {
  const response = await api.get<Blob>(`/reports/${reportId}/download`, { responseType: "blob" });
  return response.data;
}

export function describeApiError(error: unknown) {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string") return detail;
    if (error.code === "ECONNABORTED") return "The API took too long to respond.";
  }
  return error instanceof Error ? error.message : "The operation could not be completed.";
}
