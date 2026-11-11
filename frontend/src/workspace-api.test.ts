import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiClient, authenticate, disconnectApi, fetchDataset, fetchDatasetPreview, fetchTransformationHistory, getOrCreatePortfolioProjects, hasActiveSession, listDatasets, listProjects } from "./api-client";

const originalAdapter = apiClient.defaults.adapter;
function response(config: InternalAxiosRequestConfig, data: unknown): AxiosResponse {
  return { config, status: 200, statusText: "OK", headers: {}, data };
}
afterEach(async () => {
  apiClient.defaults.adapter = async (config) => response(config, null);
  await disconnectApi();
  apiClient.defaults.adapter = originalAdapter;
  vi.unstubAllGlobals();
});

describe("saved workspace API", () => {
  it("returns every existing project without creating a replacement", async () => {
    const projects = [{ id: 5, name: "New project" }, { id: 2, name: "Saved work" }];
    const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
      expect(config.method).toBe("get");
      expect(config.url).toBe("/projects");
      return response(config, projects);
    });
    apiClient.defaults.adapter = adapter;
    expect(await getOrCreatePortfolioProjects()).toEqual(projects);
    expect(adapter).toHaveBeenCalledTimes(1);
  });

  it("creates a starter project only for an empty account", async () => {
    const requests: string[] = [];
    apiClient.defaults.adapter = async (config) => {
      requests.push(`${config.method} ${config.url}`);
      return response(config, config.method === "get" ? [] : { id: 4, name: "Portfolio workspace" });
    };
    expect(await getOrCreatePortfolioProjects()).toEqual([{ id: 4, name: "Portfolio workspace" }]);
    expect(requests).toEqual(["get /projects", "post /projects"]);
  });

  it("passes cancellation to catalogue, revision, preview, and history reads", async () => {
    const controller = new AbortController();
    apiClient.defaults.adapter = async (config) => {
      expect(config.signal).toBe(controller.signal);
      if (config.url === "/datasets/9") return response(config, { id: 9 });
      return response(config, []);
    };
    await listProjects(controller.signal);
    await listDatasets(2, controller.signal);
    await fetchDataset(9, controller.signal);
    await fetchDatasetPreview(9, controller.signal);
    await fetchTransformationHistory(9, controller.signal);
  });

  it.each([0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1])("rejects invalid identifiers before making a request: %s", async (id) => {
    const adapter = vi.fn();
    apiClient.defaults.adapter = adapter;
    await expect(listDatasets(id)).rejects.toThrow("positive safe integer");
    await expect(fetchDataset(id)).rejects.toThrow("positive safe integer");
    await expect(fetchDatasetPreview(id)).rejects.toThrow("positive safe integer");
    await expect(fetchTransformationHistory(id)).rejects.toThrow("positive safe integer");
    expect(adapter).not.toHaveBeenCalled();
  });

  it("rejects mismatched projects and dataset responses", async () => {
    apiClient.defaults.adapter = async (config) => response(config, config.url === "/datasets/9" ? { id: 10 } : [{ id: 9, project_id: 3 }]);
    await expect(listDatasets(2)).rejects.toThrow("another project");
    await expect(fetchDataset(9)).rejects.toThrow("different dataset");
  });

  it("shares one token refresh across concurrent expired requests", async () => {
    let refreshCalls = 0;
    let resolveRefresh!: (value: AxiosResponse) => void;
    let refreshConfig!: InternalAxiosRequestConfig;
    apiClient.defaults.adapter = async (config) => {
      if (config.url === "/auth/login") return response(config, { access_token: "old-access", refresh_token: "old-refresh" });
      if (config.url === "/auth/refresh") {
        refreshCalls += 1;
        refreshConfig = config;
        return new Promise<AxiosResponse>((resolve) => { resolveRefresh = resolve; });
      }
      if (config.headers.Authorization !== "Bearer new-access") {
        throw new AxiosError("Expired", "ERR_BAD_REQUEST", config, undefined, { ...response(config, {}), status: 401 });
      }
      return response(config, { id: 7 });
    };
    await authenticate({ intent: "login", name: "", email: "owner@example.com", password: "safe-password-123" });
    const requests = Promise.all([fetchDataset(7), fetchDataset(7)]);
    await vi.waitFor(() => expect(refreshCalls).toBe(1));
    resolveRefresh(response(refreshConfig, { access_token: "new-access", refresh_token: "new-refresh" }));
    await expect(requests).resolves.toEqual([{ id: 7 }, { id: 7 }]);
    expect(refreshCalls).toBe(1);
  });

  it("keeps credentials in memory and revokes a login that finishes after sign-out", async () => {
    const storageWrite = vi.fn();
    vi.stubGlobal("localStorage", { setItem: storageWrite });
    vi.stubGlobal("sessionStorage", { setItem: storageWrite });
    let resolveLogin!: (response: AxiosResponse) => void;
    let loginConfig!: InternalAxiosRequestConfig;
    const revoked: string[] = [];
    apiClient.defaults.adapter = async (config) => {
      if (config.url === "/auth/login") {
        loginConfig = config;
        return new Promise<AxiosResponse>((resolve) => { resolveLogin = resolve; });
      }
      revoked.push(JSON.parse(String(config.data)).refresh_token);
      return response(config, null);
    };
    const pending = authenticate({ intent: "login", name: "", email: "owner@example.com", password: "safe-password-123" });
    await vi.waitFor(() => expect(resolveLogin).toBeDefined());
    await disconnectApi();
    resolveLogin(response(loginConfig, { access_token: "late-access", refresh_token: "late-refresh" }));
    await expect(pending).rejects.toThrow("superseded");
    expect(revoked).toEqual(["late-refresh"]);
    expect(hasActiveSession()).toBe(false);
    expect(storageWrite).not.toHaveBeenCalled();
  });
});
