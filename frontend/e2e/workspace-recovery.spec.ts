import { expect, test, type Page, type Route } from "@playwright/test";

const dataset = (id: number, projectId: number, name: string) => ({ id, project_id: projectId, original_filename: name, status: "ready", version: 3, row_count: 1, column_count: 1 });
async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}
async function connect(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Skip tour" }).click();
  const menu = page.getByRole("button", { name: "Open menu" });
  if (await menu.isVisible()) await menu.click();
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("group", { name: "Account access" }).getByRole("button", { name: "Create account", exact: true }).click();
  await page.getByLabel("Name", { exact: true }).fill("Workspace Tester");
  await page.getByLabel("Email", { exact: true }).fill("workspace@example.com");
  await page.getByLabel("New password", { exact: true }).fill("workspace12345");
  await page.getByLabel("Confirm new password", { exact: true }).fill("workspace12345");
  await page.locator(".connection-dialog form").getByRole("button", { name: "Create account", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Create your account" })).toBeHidden();
}
async function commonApi(route: Route) {
  const path = new URL(route.request().url()).pathname;
  if (path.endsWith("/auth/register")) return json(route, { access_token: "memory-access", refresh_token: "memory-refresh" });
  if (path.endsWith("/projects")) return json(route, [{ id: 1, name: "Original project" }, { id: 2, name: "Second project" }]);
  return json(route, []);
}

test("project switching ignores a late dataset catalogue response", async ({ page }) => {
  let releaseFirst!: () => void;
  let firstStarted = false;
  let firstFinished = false;
  const firstWait = new Promise<void>((resolve) => { releaseFirst = resolve; });
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/datasets/project/1")) {
      firstStarted = true;
      await firstWait;
      await json(route, [dataset(11, 1, "old-project.csv")]).catch(() => undefined);
      firstFinished = true;
      return;
    }
    if (path.endsWith("/datasets/project/2")) return json(route, [dataset(22, 2, "saved-orders.csv")]);
    if (path.endsWith("/datasets/22")) return json(route, dataset(22, 2, "saved-orders.csv"));
    if (path.endsWith("/datasets/22/preview")) return json(route, { columns: ["amount"], rows: [{ amount: 25 }], total_rows: 1, total_columns: 1, columns_truncated: 0 });
    return commonApi(route);
  });
  await connect(page);
  await expect.poll(() => firstStarted).toBe(true);
  await expect(page.getByRole("region", { name: "Saved workspace", exact: true })).toBeVisible();
  await page.getByLabel("Project", { exact: true }).selectOption("2");
  const picker = page.getByLabel("Saved dataset", { exact: true });
  await expect(picker).toBeEnabled();
  await expect(picker).toContainText("saved-orders.csv");
  releaseFirst();
  await expect.poll(() => firstFinished).toBe(true);
  await expect(picker).not.toContainText("old-project.csv");
  await picker.selectOption("22");
  await expect(page.locator(".dataset-strip")).toContainText("saved-orders.csv");
  await expect(page.locator(".dataset-strip")).toContainText("Revision 3");
  await expect(page.getByRole("cell", { name: "25", exact: true })).toBeVisible();
  const stored = await page.evaluate(() => [...Object.values(localStorage), ...Object.values(sessionStorage)].join(" "));
  expect(stored).not.toContain("memory-access");
  expect(stored).not.toContain("memory-refresh");
});

for (const [status, label] of [["FAILURE", "Failed"], ["CANCELLED", "Cancelled"], ["MONITORING_ERROR", "Monitoring unavailable"]]) {
  test(`processing card preserves ${status} without showing completion`, async ({ page }) => {
    await page.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/datasets/project/1") && route.request().method() === "POST") return json(route, { dataset_id: 11, task_id: "test-job", status: "PENDING" }, 202);
      if (path.endsWith("/datasets/jobs/test-job")) return status === "MONITORING_ERROR" ? json(route, { detail: "Monitoring unavailable" }, 403) : json(route, { task_id: "test-job", status, progress: 100, error_message: "Import did not complete." });
      return commonApi(route);
    });
    await connect(page);
    await page.getByLabel("Upload dataset file").setInputFiles({ name: "orders.csv", mimeType: "text/csv", buffer: Buffer.from("amount\n25\n") });
    const card = page.getByRole("article", { name: "Processing status" });
    await expect(card.locator(".processing-badge")).toHaveText(label);
    await expect(card).not.toContainText("100%");
    await expect(card.getByText("Complete", { exact: true })).toHaveCount(0);
    await expect(card.getByRole("progressbar")).toHaveCount(0);
  });
}
