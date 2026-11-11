import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiClient, authenticate, confirmPasswordReset, disconnectApi, fetchAccountProfile, requestPasswordReset } from "./api-client";

const originalAdapter = apiClient.defaults.adapter;
const response = (config: InternalAxiosRequestConfig, data: unknown): AxiosResponse => ({ config, status: 200, statusText: "OK", headers: {}, data });
afterEach(async () => {
  apiClient.defaults.adapter = async (config) => response(config, null);
  await disconnectApi();
  apiClient.defaults.adapter = originalAdapter;
});

describe("account APIs", () => {
  it("requests reset instructions without returning a token or asserting delivery", async () => {
    const signal = new AbortController().signal;
    apiClient.defaults.adapter = vi.fn(async (config) => {
      expect(config.url).toBe("/auth/password-reset/request");
      expect(JSON.parse(String(config.data))).toEqual({ email: "owner@example.com" });
      expect(config.signal).toBe(signal);
      return response(config, { message: "If an eligible account exists, password reset instructions will be sent." });
    });
    expect(await requestPasswordReset("owner@example.com", signal)).toBeUndefined();
  });

  it("sends a reset secret in the request body, never the URL", async () => {
    const signal = new AbortController().signal;
    apiClient.defaults.adapter = vi.fn(async (config) => {
      expect(config.url).toBe("/auth/password-reset/confirm");
      expect(config.params).toBeUndefined();
      expect(JSON.parse(String(config.data))).toEqual({ token: "one-use-token", new_password: "new-password-123" });
      expect(config.signal).toBe(signal);
      return response(config, null);
    });
    await confirmPasswordReset("one-use-token", "new-password-123", signal);
  });

  it("renews an expired access token before reloading protected account details", async () => {
    let refreshes = 0;
    apiClient.defaults.adapter = async (config) => {
      if (config.url === "/auth/login") return response(config, { access_token: "expired-access", refresh_token: "valid-refresh" });
      if (config.url === "/auth/refresh") {
        refreshes += 1;
        return response(config, { access_token: "renewed-access", refresh_token: "renewed-refresh" });
      }
      if (config.headers.Authorization !== "Bearer renewed-access") throw new AxiosError("Expired", "ERR_BAD_REQUEST", config, undefined, { ...response(config, {}), status: 401 });
      return response(config, { id: 3, name: "Ana", email: "ana@example.com" });
    };
    await authenticate({ intent: "login", name: "", email: "ana@example.com", password: "valid-password-123" });
    expect(await fetchAccountProfile()).toMatchObject({ id: 3, email: "ana@example.com" });
    expect(refreshes).toBe(1);
  });

  it("loads account details with cancellation support", async () => {
    const profile = { id: 3, name: "Ana", email: "ana@example.com", created_at: "2026-11-12T00:00:00Z" };
    const signal = new AbortController().signal;
    apiClient.defaults.adapter = async (config) => {
      expect(config.url).toBe("/auth/me");
      expect(config.signal).toBe(signal);
      return response(config, profile);
    };
    expect(await fetchAccountProfile(signal)).toEqual(profile);
  });
});
