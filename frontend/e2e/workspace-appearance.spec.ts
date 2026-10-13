import { expect, test, type Page, type TestInfo } from "@playwright/test";

async function openSection(page: Page, name: string) {
  const menu = page.getByRole("button", { name: "Open menu", exact: true });
  const mobileMenuVisible = await menu.isVisible();
  const navigation = page.getByRole("navigation", { name: "Workspace", exact: true, includeHidden: true });
  if (mobileMenuVisible) {
    await menu.click();
    await expect(menu).toHaveAttribute("aria-expanded", "true");
    await expect(navigation).toBeInViewport();
  }
  await navigation.getByRole("button", { name, exact: true }).click();
  if (mobileMenuVisible) {
    await expect(menu).toHaveAttribute("aria-expanded", "false");
    await expect(navigation).not.toBeInViewport();
  }
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
}

async function expectNoPageOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
}

async function capture(page: Page, testInfo: TestInfo, name: string) {
  await page.evaluate(() => document.fonts.ready);
  const screenshotPath = testInfo.outputPath(`${name}.png`);
  await page.screenshot({ path: screenshotPath, fullPage: true, animations: "disabled" });
  await testInfo.attach(name, { path: screenshotPath, contentType: "image/png" });
}

test("workspace navigation, responsive layout, and persistent themes remain usable", async ({ page, isMobile }, testInfo) => {
  const runtimeErrors: string[] = [];
  page.on("pageerror", (error) => runtimeErrors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") runtimeErrors.push(message.text());
  });

  await page.goto("/");
  await expect(page).toHaveURL("http://127.0.0.1:4173/");
  await expect(page).toHaveTitle(/Data Analytics Platform|DataFlow/i);
  await page.getByRole("button", { name: "Skip tour", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.locator("vite-error-overlay")).toHaveCount(0);
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  if (isMobile) {
    const menu = page.getByRole("button", { name: "Open menu", exact: true });
    await menu.click();
    await expect(menu).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByRole("navigation", { name: "Workspace", exact: true })).toBeInViewport();
    await page.keyboard.press("Escape");
    await expect(menu).toHaveAttribute("aria-expanded", "false");
    await expect(menu).toBeFocused();
    await expect(page.getByRole("navigation", { name: "Workspace", exact: true, includeHidden: true })).not.toBeInViewport();
  }
  await expectNoPageOverflow(page);
  await capture(page, testInfo, `overview-dark-${isMobile ? "mobile" : "desktop"}`);

  await openSection(page, "Datasets");
  await expect(page.getByRole("heading", { name: /^Data preview/ })).toBeVisible();
  const previewHeaders = page.locator(".table-panel th:not(.preview-row-number)");
  await page.getByRole("textbox", { name: "Search columns", exact: true }).fill("customer");
  await expect(previewHeaders).toHaveCount(1);
  await expect(previewHeaders).toContainText("customer");
  await page.getByRole("textbox", { name: "Search columns", exact: true }).fill("no_matching_column");
  await expect(page.getByText("No matching columns", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Clear search", exact: true }).click();
  await expect(previewHeaders).toHaveCount(8);
  await expectNoPageOverflow(page);

  await openSection(page, "Transformations");
  await page.getByRole("button", { name: "Preview impact", exact: true }).click();
  await expect(page.locator(".comparison")).toContainText("6 rows");
  await expect(page.locator(".comparison")).toContainText("5 rows");
  await expectNoPageOverflow(page);
  if (!isMobile) await capture(page, testInfo, "transformations-dark-desktop");

  await openSection(page, "Dashboards");
  await page.getByRole("button", { name: "Build chart", exact: true }).click();
  await expect(page.getByRole("img", { name: "Generated data chart", exact: true })).toBeVisible();
  await expectNoPageOverflow(page);

  await openSection(page, "Reports");
  await expect(page.getByRole("heading", { name: "PDF reports", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Generate PDF", exact: true })).toBeDisabled();
  await expectNoPageOverflow(page);

  await openSection(page, "Monitoring");
  await expect(page.getByRole("heading", { name: "Runtime monitoring", exact: true })).toBeVisible();
  await expect(page.getByText("No jobs yet", { exact: true })).toBeVisible();
  await expectNoPageOverflow(page);

  await openSection(page, "Overview");
  await page.getByRole("button", { name: "Switch to light theme", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  await expectNoPageOverflow(page);
  if (!isMobile) await capture(page, testInfo, "overview-light-desktop");
  await openSection(page, "Transformations");
  await page.getByRole("button", { name: "Preview impact", exact: true }).click();
  await expect(page.locator(".comparison")).toContainText("5 rows");
  await expectNoPageOverflow(page);
  if (!isMobile) await capture(page, testInfo, "transformations-light-desktop");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  const skipTour = page.getByRole("button", { name: "Skip tour", exact: true });
  if (await skipTour.isVisible()) await skipTour.click();
  await page.getByRole("button", { name: "Switch to dark theme", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator("vite-error-overlay")).toHaveCount(0);
  expect(runtimeErrors).toEqual([]);
});
