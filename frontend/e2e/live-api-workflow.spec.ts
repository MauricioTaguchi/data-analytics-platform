import path from "node:path";
import { expect, test } from "@playwright/test";

const liveTest = process.env.E2E_LIVE_API === "true" ? test : test.skip;

liveTest("authenticated workflow reaches dashboard and PDF report", async ({ page }) => {
  const email = `reviewer-${Date.now()}-${test.info().retry}@example.com`;
  await page.goto("/");
  await page.getByRole("button", { name: "Skip tour" }).click();
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
  await expect(page.getByText("Live API", { exact: true })).toBeVisible({ timeout: 15_000 });

  await page.locator('input[type="file"]').setInputFiles(path.resolve("../docs/sample-data/sales.csv"));
  await expect(page.getByText("Import and profiling completed through the worker queue.")).toBeVisible({ timeout: 30_000 });

  await page.getByRole("button", { name: "04 Dashboards" }).click();
  await page.getByRole("button", { name: "Build chart" }).click();
  await expect(page.getByRole("img", { name: "Generated data chart" })).toBeVisible();

  await page.getByRole("button", { name: "05 Reports" }).click();
  await page.getByRole("button", { name: "Generate PDF" }).click();
  await expect(page.getByText("The PDF report is ready to download.")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: "Download" }).first()).toBeEnabled();

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByText("The server session was revoked and the local demo was restored.")).toBeVisible();
});
