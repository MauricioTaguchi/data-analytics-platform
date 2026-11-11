import { expect, test, type Page, type Route } from "@playwright/test";

const profile = { id: 3, name: "Ana Silva", email: "ana@example.com", created_at: "2026-11-12T00:00:00Z" };
const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
async function defaultApi(route: Route) {
  const path = new URL(route.request().url()).pathname;
  if (path.endsWith("/auth/login") || path.endsWith("/auth/register")) return json(route, { access_token: "account-access", refresh_token: "account-refresh" });
  if (path.endsWith("/auth/me")) return json(route, profile);
  if (path.endsWith("/projects")) return json(route, [{ id: 1, name: "My project" }]);
  if (path.endsWith("/auth/logout")) return route.fulfill({ status: 204 });
  return json(route, []);
}
async function openSignIn(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Skip tour" }).click();
  const menu = page.getByRole("button", { name: "Open menu" });
  if (await menu.isVisible()) await menu.click();
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Sign in to DataFlow", exact: true });
  await expect(dialog.getByLabel("Email", { exact: true })).toBeFocused();
  return dialog;
}

test("sign-in errors stay in the dialog, account details can retry, and reset requests stay private", async ({ page }, testInfo) => {
  let logins = 0;
  let profileAttempts = 0;
  let resetRequests = 0;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/login") && logins++ === 0) return json(route, { detail: "Invalid credentials" }, 401);
    if (path.endsWith("/auth/me") && profileAttempts++ === 0) return json(route, { detail: "Temporarily unavailable" }, 503);
    if (path.endsWith("/auth/password-reset/request")) {
      expect(route.request().postDataJSON()).toEqual({ email: "ana@example.com" });
      resetRequests += 1;
      return json(route, { message: "If an eligible account exists, password reset instructions will be sent." }, 202);
    }
    return defaultApi(route);
  });
  const dialog = await openSignIn(page);
  await dialog.getByLabel("Email", { exact: true }).fill(profile.email);
  await dialog.getByLabel("Password", { exact: true }).fill("wrong-password-123");
  await dialog.locator("form").getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("Email or password is incorrect");
  await expect(dialog.getByRole("alert")).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("sign-in-error.png"), fullPage: true });
  await expect(dialog.getByLabel("Email", { exact: true })).toHaveValue(profile.email);
  await dialog.getByLabel("Password", { exact: true }).fill("valid-password-123");
  await dialog.locator("form").getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(dialog).toBeHidden();
  await page.getByRole("button", { name: "Account", exact: true }).click();
  const account = page.getByRole("dialog", { name: "Your account", exact: true });
  await expect(account.getByRole("alert")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("account-load-error.png"), fullPage: true });
  await account.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(account.getByText(profile.name, { exact: true })).toBeVisible();
  await expect(account.getByText(profile.email, { exact: true })).toBeVisible();
  await account.getByRole("button", { name: "Reset password", exact: true }).click();
  const recovery = page.getByRole("dialog", { name: "Reset your password", exact: true });
  await expect(recovery.getByLabel("Email", { exact: true })).toHaveValue(profile.email);
  await recovery.getByRole("button", { name: "Request reset link", exact: true }).click();
  await expect(recovery.getByRole("status")).toContainText("If an eligible account exists");
  expect(resetRequests).toBe(1);
  await expect(recovery).not.toContainText("Email sent");
  await page.screenshot({ path: testInfo.outputPath("password-reset-request.png"), fullPage: true });
  await page.keyboard.press("Escape");
  await expect(recovery).toBeHidden();
});

test("a reset link is removed from the address, validates passwords, and returns to sign in", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let confirmations = 0;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/password-reset/confirm")) {
      expect(route.request().postDataJSON()).toEqual({ token: "one-use-reset-secret", new_password: "new-password-123" });
      confirmations += 1;
      return route.fulfill({ status: 204 });
    }
    return defaultApi(route);
  });
  await page.goto("/#reset_token=one-use-reset-secret");
  await expect(page).toHaveURL("http://127.0.0.1:4173/");
  const reset = page.getByRole("dialog", { name: "Choose a new password", exact: true });
  await expect(reset.getByLabel("New password", { exact: true })).toBeFocused();
  await expect(page.getByRole("dialog", { name: "Start with trustworthy data" })).toHaveCount(0);
  const savePassword = reset.getByRole("button", { name: "Save new password", exact: true });
  await savePassword.focus();
  await page.keyboard.press("Tab");
  await expect(reset.getByRole("button", { name: "Close", exact: true })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(savePassword).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("reset-password-keyboard-focus.png"), fullPage: true });
  await reset.getByLabel("New password", { exact: true }).fill("new-password-123");
  await reset.getByLabel("Confirm new password", { exact: true }).fill("mismatched-password-123");
  await reset.getByRole("button", { name: "Save new password", exact: true }).click();
  await expect(reset.getByRole("alert")).toContainText("do not match");
  await expect(reset.getByRole("alert")).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("reset-password-validation.png"), fullPage: true });
  expect(confirmations).toBe(0);
  await reset.getByLabel("Confirm new password", { exact: true }).fill("new-password-123");
  await reset.getByRole("button", { name: "Show password", exact: true }).click();
  await expect(reset.getByLabel("New password", { exact: true })).toHaveAttribute("type", "text");
  await reset.getByRole("button", { name: "Save new password", exact: true }).click();
  const login = page.getByRole("dialog", { name: "Sign in to DataFlow", exact: true });
  await expect(login.getByRole("status")).toHaveText("Your password was updated. Sign in with your new password.");
  await expect(login.getByLabel("Password", { exact: true })).toHaveValue("");
  await expect(login.getByLabel("Email", { exact: true })).toBeFocused();
  expect(confirmations).toBe(1);
  await login.getByLabel("Email", { exact: true }).fill(profile.email);
  await login.getByLabel("Password", { exact: true }).fill("new-password-123");
  await login.locator("form").getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(login).toBeHidden();
  await page.getByRole("button", { name: "Account", exact: true }).click();
  const account = page.getByRole("dialog", { name: "Your account", exact: true });
  await expect(account.getByText(profile.name, { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("account-after-password-reset.png"), fullPage: true });
  await account.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(account).toBeHidden();
  await expect(page.locator(".dataset-heading")).toContainText("Demo mode");
  const stored = await page.evaluate(() => [...Object.values(localStorage), ...Object.values(sessionStorage)].join(" "));
  for (const secret of ["one-use-reset-secret", "new-password-123", "account-access", "account-refresh"]) expect(stored).not.toContain(secret);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
  expect(errors).toEqual([]);
});

test("an expired reset link provides a clear way to request another", async ({ page }, testInfo) => {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/auth/password-reset/confirm")) return json(route, { detail: "Invalid or expired password reset token." }, 400);
    if (path.endsWith("/auth/password-reset/request")) return json(route, { message: "If an eligible account exists, password reset instructions will be sent." }, 202);
    return defaultApi(route);
  });
  await page.goto("/#reset_token=expired-reset-secret");
  const reset = page.getByRole("dialog", { name: "Choose a new password", exact: true });
  await reset.getByLabel("New password", { exact: true }).fill("new-password-123");
  await reset.getByLabel("Confirm new password", { exact: true }).fill("new-password-123");
  await reset.getByRole("button", { name: "Save new password", exact: true }).click();
  await expect(reset.getByRole("alert")).toContainText("invalid or has expired");
  await page.screenshot({ path: testInfo.outputPath("reset-link-expired.png"), fullPage: true });
  await reset.getByRole("button", { name: "Request a new reset link", exact: true }).click();
  const recovery = page.getByRole("dialog", { name: "Reset your password", exact: true });
  await expect(recovery.getByLabel("Email", { exact: true })).toBeFocused();
  await recovery.getByLabel("Email", { exact: true }).fill("unknown@example.com");
  await recovery.getByRole("button", { name: "Request reset link", exact: true }).click();
  await expect(recovery.getByRole("status")).toContainText("If an eligible account exists");
  await recovery.getByRole("button", { name: "Back to sign in", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Sign in to DataFlow", exact: true })).toBeVisible();
});
