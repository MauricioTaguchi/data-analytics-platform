import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiClient } from "./api-client";
import { deleteSavedResource, deletionErrorMessage } from "./lifecycle-api";

const originalAdapter = apiClient.defaults.adapter;
const response = (config: InternalAxiosRequestConfig, data: unknown): AxiosResponse => ({ config, status: 202, statusText: "Accepted", headers: {}, data });
afterEach(() => { apiClient.defaults.adapter = originalAdapter; });

describe("explicit saved-resource deletion", () => {
  it.each(["dataset", "project"] as const)("sends confirmed %s deletion to its protected endpoint", async (scope) => {
    const receipt = { status: "deleted", scope, cleanup_pending: true, message: "Removed from workspace." };
    apiClient.defaults.adapter = async (config) => {
      expect(config.url).toBe(`/lifecycle/${scope}s/7`);
      expect(config.method).toBe("delete");
      expect(JSON.parse(String(config.data))).toEqual({ confirmation: "DELETE" });
      return response(config, receipt);
    };
    expect(await deleteSavedResource({ scope, id: 7, name: "Saved item" }, "DELETE")).toEqual(receipt);
  });

  it("puts account reauthentication only in the request body", async () => {
    apiClient.defaults.adapter = async (config) => {
      expect(config.url).toBe("/lifecycle/account");
      expect(config.params).toBeUndefined();
      expect(JSON.parse(String(config.data))).toEqual({ confirmation: "DELETE", current_password: "private-password-123" });
      return response(config, { status: "deleted", scope: "account", cleanup_pending: true });
    };
    await deleteSavedResource({ scope: "account", name: "owner@example.com" }, "DELETE", "private-password-123");
  });

  it("does not send incomplete confirmation, password or invalid identifiers", async () => {
    const adapter = vi.fn();
    apiClient.defaults.adapter = adapter;
    await expect(deleteSavedResource({ scope: "dataset", id: 1, name: "A" }, "delete")).rejects.toThrow("Type DELETE");
    await expect(deleteSavedResource({ scope: "account", name: "A" }, "DELETE")).rejects.toThrow("current password");
    await expect(deleteSavedResource({ scope: "project", id: -1, name: "A" }, "DELETE")).rejects.toThrow("positive safe integer");
    expect(adapter).not.toHaveBeenCalled();
  });

  it.each([
    [409, "project", "Cancellation must finish"],
    [403, "account", "current password was not accepted"],
    [404, "dataset", "no longer available"],
    [503, "dataset", "deletion may have been accepted"],
  ] as const)("explains %s without claiming rejected changes were applied", (status, scope, expected) => {
    const failure = new AxiosError("Request failed", undefined, undefined, undefined, { status, data: {}, headers: {}, statusText: "", config: {} as InternalAxiosRequestConfig });
    expect(deletionErrorMessage(failure, scope)).toContain(expected);
  });

  it("preserves uncertainty after a lost response and gives a safe recovery step", () => {
    expect(deletionErrorMessage(new AxiosError("Network error"), "account")).toContain("check whether you can still sign in");
    expect(deletionErrorMessage(new Error("Type DELETE exactly to confirm."), "project")).toBe("Type DELETE exactly to confirm.");
  });
});
