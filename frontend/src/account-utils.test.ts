import { AxiosError, type AxiosResponse } from "axios";
import { describe, expect, it, vi } from "vitest";
import { accountErrorMessage, consumePasswordResetToken, passwordValidationMessage } from "./account-utils";

describe("account form safeguards", () => {
  it("checks new passwords without trimming or truncating them", () => {
    expect(passwordValidationMessage("safe-password-123", "safe-password-123")).toBeNull();
    expect(passwordValidationMessage("safe-password", "safe-password")).toContain("letter and a number");
    expect(passwordValidationMessage("short1", "short1")).toContain("10 characters");
    expect(passwordValidationMessage("safe-password-123", "different-password-123")).toContain("do not match");
    expect(passwordValidationMessage(`a1${"🔒".repeat(20)}`, `a1${"🔒".repeat(20)}`)).toContain("too long");
    expect(passwordValidationMessage("  safe-password-123  ", "safe-password-123")).toContain("do not match");
  });

  it("consumes a reset token from the fragment and removes it from browser history", () => {
    const history = { state: { view: "workspace" }, replaceState: vi.fn() };
    expect(consumePasswordResetToken({ href: "https://example.com/?source=email#reset_token=one-use-token&view=account" }, history)).toBe("one-use-token");
    expect(history.replaceState).toHaveBeenCalledWith(history.state, "", "/?source=email#view=account");
  });

  it("leaves ordinary URLs alone and treats an empty reset link as incomplete", () => {
    const history = { state: null, replaceState: vi.fn() };
    expect(consumePasswordResetToken({ href: "https://example.com/#overview" }, history)).toBeNull();
    expect(history.replaceState).not.toHaveBeenCalled();
    expect(consumePasswordResetToken({ href: "https://example.com/#reset_token=" }, history)).toBe("");
    expect(history.replaceState).toHaveBeenCalledWith(null, "", "/");
  });

  it.each([
    ["login", 401, "Email or password is incorrect"],
    ["register", 409, "already registered"],
    ["reset", 400, "invalid or has expired"],
    ["forgot", 429, "Too many attempts"],
    ["forgot", 503, "try again shortly"],
  ] as const)("provides an actionable %s error for %s without exposing server internals", (mode, status, expected) => {
    const error = new AxiosError("Internal details", undefined, undefined, undefined, { status, data: { detail: "secret-token" } } as AxiosResponse);
    expect(accountErrorMessage(error, mode)).toContain(expected);
    expect(accountErrorMessage(error, mode)).not.toContain("secret-token");
  });
});
