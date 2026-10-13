import path from "node:path";
import { expect, test, type Page } from "@playwright/test";

const liveTest = process.env.E2E_LIVE_API === "true" ? test : test.skip;

async function openMenu(page: Page) {
  const menu = page.getByRole("button", { name: "Open menu" });
  if (await menu.isVisible() && await page.locator(".sidebar.open").count() === 0) await menu.click();
}

async function openSection(page: Page, name: string) {
  await openMenu(page);
  await page.getByRole("button", { name }).click();
}

liveTest("authenticated workflow persists transformations and undo before dashboard and PDF report", async ({ page }) => {
  test.setTimeout(120_000);
  const email = `reviewer-${Date.now()}-${test.info().retry}@example.com`;
  await page.goto("/");
  await page.getByRole("button", { name: "Skip tour" }).click();
  await openMenu(page);
  await page.getByRole("button", { name: "Connect API" }).click();
  await page.getByLabel("Name").fill("Portfolio Reviewer");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("portfolio12345");

  const registrationResponsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/auth/register") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Create and connect" }).click();
  const registrationResponse = await registrationResponsePromise;
  expect(registrationResponse.status(), await registrationResponse.text()).toBe(201);
  const { access_token: accessToken } = await registrationResponse.json();
  const apiUrl = registrationResponse.url().replace(/\/auth\/register$/, "");
  const headers = { Authorization: `Bearer ${accessToken}` };
  await expect(page.getByText("Live API", { exact: true })).toBeVisible({ timeout: 15_000 });

  const uploadResponsePromise = page.waitForResponse(
    (response) => /\/datasets\/project\/\d+$/.test(response.url()) && response.request().method() === "POST",
  );
  await page.locator('input[type="file"]').setInputFiles(path.resolve("../docs/sample-data/sales.csv"));
  const uploadResponse = await uploadResponsePromise;
  expect(uploadResponse.status(), await uploadResponse.text()).toBe(202);
  const { dataset_id: datasetId } = await uploadResponse.json();
  const datasetUrl = `${apiUrl}/datasets/${datasetId}`;
  await expect(page.getByText("Import and profiling completed through the worker queue.")).toBeVisible({ timeout: 30_000 });

  const initialResponse = await page.request.get(datasetUrl, { headers });
  expect(initialResponse.ok()).toBe(true);
  const initialDataset = await initialResponse.json();
  const initialPreviewResponse = await page.request.get(`${datasetUrl}/preview`, { headers });
  expect(initialPreviewResponse.ok()).toBe(true);
  const initialPreview = await initialPreviewResponse.json();

  await openSection(page, "03 Transformations");
  await page.getByRole("button", { name: "Rename column", exact: true }).click();
  await page.getByLabel("Column", { exact: true }).selectOption("customer");
  await page.getByLabel("Parameter", { exact: true }).fill("customer_name");
  await page.getByRole("button", { name: "Apply transformation" }).click();
  await expect(page.getByText("The version-controlled transformation completed successfully.")).toBeVisible({ timeout: 30_000 });
  const transformedResponse = await page.request.get(`${datasetUrl}/preview`, { headers });
  expect(transformedResponse.ok()).toBe(true);
  const transformedPreview = await transformedResponse.json();
  expect(transformedPreview.columns).toContain("customer_name");
  expect(transformedPreview.columns).not.toContain("customer");

  const undoResponsePromise = page.waitForResponse(
    (response) => response.url() === `${datasetUrl}/transformations/undo` && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Undo latest transformation" }).click();
  const undoResponse = await undoResponsePromise;
  expect(undoResponse.status(), await undoResponse.text()).toBe(200);
  const receipt = await undoResponse.json();
  const undoKey = undoResponse.request().headers()["idempotency-key"];
  expect(undoKey).toBeTruthy();
  expect(receipt.previous_version).toBe(initialDataset.version + 1);
  expect(receipt.result_version).toBe(initialDataset.version + 2);
  await expect(page.getByText("The latest completed transformation was reversed.")).toBeVisible();
  await expect(page.locator(".timeline")).toContainText("undone");

  const replayResponse = await page.request.post(`${datasetUrl}/transformations/undo`, {
    headers: { ...headers, "Idempotency-Key": undoKey },
    data: { expected_version: receipt.previous_version },
  });
  expect(replayResponse.status(), await replayResponse.text()).toBe(200);
  expect(await replayResponse.json()).toEqual(receipt);
  const restoredResponse = await page.request.get(datasetUrl, { headers });
  expect(restoredResponse.ok()).toBe(true);
  expect((await restoredResponse.json()).version).toBe(receipt.result_version);
  const restoredPreviewResponse = await page.request.get(`${datasetUrl}/preview`, { headers });
  expect(restoredPreviewResponse.ok()).toBe(true);
  expect(await restoredPreviewResponse.json()).toEqual(initialPreview);

  await openSection(page, "04 Dashboards");
  await page.getByRole("button", { name: "Build chart" }).click();
  await expect(page.getByRole("img", { name: "Generated data chart" })).toBeVisible();

  await openSection(page, "05 Reports");
  await page.getByRole("button", { name: "Generate PDF" }).click();
  await expect(page.getByText("The PDF report is ready to download.")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: "Download" }).first()).toBeEnabled();

  await openMenu(page);
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByText("The server session was revoked, active work was cancelled, and the local demo was restored.")).toBeVisible();
});
