import { expect, test, type Page, type Route } from "@playwright/test";

const source = { id: 11, project_id: 1, original_filename: "orders.csv", status: "ready", version: 3, row_count: 1, column_count: 1 };
const json = (route: Route, data: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });

async function setup(page: Page, deletion?: (route: Route) => Promise<void>) {
  const state = { projects: [{ id: 1, name: "Saved project" }], datasets: [source], creations: 0, deletions: 0 };
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (path.includes("/lifecycle/")) {
      state.deletions += 1;
      if (deletion) return deletion(route);
      if (path.endsWith("/projects/1")) { state.projects = []; state.datasets = []; }
      else if (path.endsWith("/datasets/11")) state.datasets = [];
      return json(route, { status: "deleted", scope: path.includes("datasets") ? "dataset" : path.includes("projects") ? "project" : "account", cleanup_pending: true, message: "Removed." }, 202);
    }
    if (path.endsWith("/auth/login")) return json(route, { access_token: "lifecycle-access", refresh_token: "lifecycle-refresh" });
    if (path.endsWith("/auth/me")) return json(route, { id: 1, name: "Workspace owner", email: "owner@example.com", created_at: "2026-11-11T00:00:00Z" });
    if (path.endsWith("/auth/logout")) return route.fulfill({ status: 204 });
    if (path.endsWith("/projects") && method === "POST") {
      state.creations += 1;
      const created = { id: 2, name: route.request().postDataJSON().name };
      state.projects.push(created);
      return json(route, created, 201);
    }
    if (path.endsWith("/projects")) return json(route, state.projects);
    if (path.endsWith("/datasets/project/1")) return json(route, state.datasets);
    if (path.endsWith("/datasets/11/preview")) return json(route, { columns: ["amount"], rows: [{ amount: 25 }], total_rows: 1, total_columns: 1, columns_truncated: 0 });
    if (path.endsWith("/datasets/11")) return json(route, source);
    return json(route, []);
  });
  await signIn(page);
  return state;
}

async function signIn(page: Page) {
  await page.goto("/");
  await expect(page).toHaveTitle("Data Analytics Platform");
  await page.getByRole("button", { name: "Skip tour" }).click();
  const menu = page.getByRole("button", { name: "Open menu" });
  if (await menu.isVisible()) await menu.click();
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const login = page.getByRole("dialog", { name: "Sign in to DataFlow", exact: true });
  await login.getByLabel("Email", { exact: true }).fill("owner@example.com");
  await login.getByLabel("Password", { exact: true }).fill("valid-password-123");
  await login.locator("form").getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(login).toBeHidden();
  await expect(page.getByRole("region", { name: "Saved workspace", exact: true })).toBeVisible();
}

test("deletion requires explicit confirmation, cancels safely and restores keyboard focus", async ({ page }, testInfo) => {
  const state = await setup(page);
  const trigger = page.getByRole("button", { name: "Delete project", exact: true });
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "Delete project?", exact: true });
  await expect(dialog.getByRole("button", { name: "Keep project", exact: true })).toBeFocused();
  const submit = dialog.getByRole("button", { name: "Delete project", exact: true });
  await expect(submit).toBeDisabled();
  await dialog.getByLabel("Type DELETE to confirm", { exact: true }).fill("delete");
  await expect(submit).toBeDisabled();
  await dialog.getByLabel("Type DELETE to confirm", { exact: true }).fill("DELETE");
  await submit.focus();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "Close deletion dialog" })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(submit).toBeFocused();
  await expect(dialog).toContainText("existing backups remain subject");
  await page.screenshot({ path: testInfo.outputPath("deletion-keyboard-confirmation.png"), fullPage: true });
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
  expect(state.deletions).toBe(0);
  await expect(page.getByLabel("Project", { exact: true })).toHaveValue("1");
});

test("active jobs block deletion and a successful retry clears the selected dataset", async ({ page }, testInfo) => {
  let blocked = true;
  const state = await setup(page, async (route) => {
    expect(route.request().method()).toBe("DELETE");
    expect(route.request().postDataJSON()).toEqual({ confirmation: "DELETE" });
    if (blocked) return json(route, { detail: "Wait for active jobs to finish or cancel them before deleting data." }, 409);
    state.datasets = [];
    return json(route, { status: "deleted", scope: "dataset", cleanup_pending: true }, 202);
  });
  await page.getByLabel("Saved dataset", { exact: true }).selectOption("11");
  await expect(page.locator(".dataset-strip")).toContainText("orders.csv");
  await page.getByRole("button", { name: "Delete dataset", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Delete dataset?", exact: true });
  await dialog.getByLabel("Type DELETE to confirm", { exact: true }).fill("DELETE");
  await dialog.getByRole("button", { name: "Delete dataset", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("Cancellation must finish");
  await expect(dialog.getByRole("alert")).toBeFocused();
  await expect(page.locator(".dataset-strip")).toContainText("orders.csv");
  // A repeated rejection with the same message must restore alert focus too.
  await dialog.getByRole("button", { name: "Delete dataset", exact: true }).click();
  await expect.poll(() => state.deletions).toBe(2);
  await expect(dialog.getByRole("alert")).toContainText("Cancellation must finish");
  await expect(dialog.getByRole("alert")).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("deletion-active-job-error.png"), fullPage: true });
  blocked = false;
  await dialog.getByRole("button", { name: "Delete dataset", exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect(page.locator(".dataset-strip")).toContainText("sales_sample.csv");
  await expect(page.getByLabel("Saved dataset", { exact: true })).not.toContainText("orders.csv");
  await expect(page.getByRole("button", { name: "Delete dataset", exact: true })).toBeDisabled();
  await expect(page.locator(".notice")).toContainText("File cleanup is pending");
  await expect(page.getByRole("heading", { name: "Analytics overview", exact: true })).toBeFocused();
  expect(state.deletions).toBe(3);
});

test("deleting the last project keeps the account signed in without recreating data automatically", async ({ page }, testInfo) => {
  const state = await setup(page);
  await page.getByRole("button", { name: "Delete project", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Delete project?", exact: true });
  await dialog.getByLabel("Type DELETE to confirm", { exact: true }).fill("DELETE");
  await dialog.getByRole("button", { name: "Delete project", exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect(page.locator(".dataset-heading")).toContainText("Signed in");
  await expect(page.getByRole("region", { name: "Saved workspace" })).toContainText("no saved projects");
  await expect(page.getByLabel("Project", { exact: true })).toBeDisabled();
  expect(state.creations).toBe(0);
  await page.screenshot({ path: testInfo.outputPath("empty-signed-in-workspace.png"), fullPage: true });
  // Reload clears the memory-only session. A subsequent login must not silently
  // recreate a project the user explicitly removed.
  await signIn(page);
  await expect(page.getByLabel("Project", { exact: true })).toHaveValue("");
  expect(state.creations).toBe(0);
  await page.getByRole("button", { name: "Create project", exact: true }).click();
  await expect(page.getByLabel("Project", { exact: true })).toHaveValue("2");
  await expect(page.getByLabel("Project", { exact: true })).toContainText("New project");
  expect(state.creations).toBe(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
});

test("account deletion reauthenticates, protects passwords and leaves a signed-out sample", async ({ page }, testInfo) => {
  let attempts = 0;
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await setup(page, async (route) => {
    const body = route.request().postDataJSON();
    expect(body.confirmation).toBe("DELETE");
    expect(route.request().url()).not.toContain("password");
    attempts += 1;
    if (attempts === 1) return json(route, { detail: "Current password is incorrect." }, 403);
    expect(body.current_password).toBe("correct-password-123");
    return json(route, { status: "deleted", scope: "account", cleanup_pending: true }, 202);
  });
  await page.getByRole("button", { name: "Account", exact: true }).click();
  await page.getByRole("dialog", { name: "Your account", exact: true }).getByRole("button", { name: "Delete account", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Delete account?", exact: true });
  const password = dialog.getByLabel("Current password", { exact: true });
  const submit = dialog.getByRole("button", { name: "Delete account", exact: true });
  await dialog.getByLabel("Type DELETE to confirm", { exact: true }).fill("DELETE");
  await expect(submit).toBeDisabled();
  await password.fill("wrong-password-123");
  await submit.click();
  await expect(dialog.getByRole("alert")).toContainText("password was not accepted");
  await expect(password).toHaveValue("");
  await expect(dialog.getByRole("alert")).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("account-deletion-reauthentication.png"), fullPage: true });
  await password.fill("correct-password-123");
  await submit.click();
  await expect(dialog).toBeHidden();
  await expect(page.locator(".dataset-heading")).toContainText("Demo mode");
  await expect(page.getByRole("button", { name: "Account", exact: true })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Saved workspace" })).toHaveCount(0);
  await expect(page.locator(".notice")).toContainText("account is closed");
  const stored = await page.evaluate(() => [...Object.values(localStorage), ...Object.values(sessionStorage)].join(" "));
  for (const secret of ["wrong-password-123", "correct-password-123", "lifecycle-access", "lifecycle-refresh"]) expect(stored).not.toContain(secret);
  expect(errors).toEqual([]);
});

test("an uncertain deletion response keeps the resource visible and offers reconciliation", async ({ page }) => {
  const state = await setup(page, async (route) => {
    // The server accepted deletion but its response was lost at the gateway.
    state.projects = [];
    state.datasets = [];
    return json(route, { detail: "Unavailable" }, 503);
  });
  await page.getByRole("button", { name: "Delete project", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Delete project?", exact: true });
  await dialog.getByLabel("Type DELETE to confirm", { exact: true }).fill("DELETE");
  await dialog.getByRole("button", { name: "Delete project", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("deletion may have been accepted");
  await expect(dialog.getByRole("alert")).toBeFocused();
  await expect(page.getByLabel("Project", { exact: true })).toHaveValue("1");
  await dialog.getByRole("button", { name: "Keep project", exact: true }).click();
  await expect(page.getByRole("button", { name: "Delete project", exact: true })).toBeFocused();
  await page.getByRole("button", { name: "Refresh list", exact: true }).click();
  await expect(page.getByLabel("Project", { exact: true })).toHaveValue("");
  await expect(page.getByRole("region", { name: "Saved workspace" })).toContainText("no saved projects");
  expect(state.deletions).toBe(1);
});

test("pending deletion cannot be dismissed or submitted a second time", async ({ page }) => {
  let release!: () => void;
  const responseReady = new Promise<void>((resolve) => { release = resolve; });
  const state = await setup(page, async (route) => {
    await responseReady;
    state.projects = [];
    return json(route, { status: "deleted", scope: "project", cleanup_pending: true }, 202);
  });
  await page.getByRole("button", { name: "Delete project", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Delete project?", exact: true });
  await dialog.getByLabel("Type DELETE to confirm", { exact: true }).fill("DELETE");
  await dialog.getByRole("button", { name: "Delete project", exact: true }).click();
  await expect(dialog).toHaveAttribute("aria-busy", "true");
  await expect(dialog.getByRole("button", { name: "Deleting…", exact: true })).toBeDisabled();
  await expect(dialog.getByRole("button", { name: "Keep project", exact: true })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  expect(state.deletions).toBe(1);
  release();
  await expect(dialog).toBeHidden();
  expect(state.deletions).toBe(1);
});
