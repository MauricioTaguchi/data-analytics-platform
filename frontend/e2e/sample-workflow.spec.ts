import { expect, test, type Page } from "@playwright/test";

async function openSection(page: Page, name: string) {
  const mobileMenu = page.getByRole("button", { name: "Open menu" });
  if (await mobileMenu.isVisible()) await mobileMenu.click();
  await page.getByRole("navigation", { name: "Workspace", exact: true }).getByRole("button", { name, exact: true }).click();
}

test("guided tour leads into a reversible sample transformation", async ({ page }) => {
  await page.goto("/");

  const tour = page.getByRole("dialog");
  await expect(tour.getByRole("heading", { name: "Start with trustworthy data" })).toBeVisible();
  const next = tour.getByRole("button", { name: "Next", exact: true });
  await next.focus();
  await page.keyboard.press("Tab");
  await expect(tour.getByRole("button", { name: "Skip tour", exact: true })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(next).toBeFocused();
  await next.click();
  await next.click();
  await tour.getByRole("button", { name: "Explore DataFlow", exact: true }).click();

  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.getByRole("button", { name: "Export current version" })).toBeVisible();

  await openSection(page, "Transformations");
  await page.getByRole("button", { name: "Preview impact" }).click();
  await expect(page.locator(".comparison")).toContainText("6 rows");
  await expect(page.locator(".comparison")).toContainText("5 rows");

  await page.getByRole("button", { name: "Apply transformation" }).click();
  await expect(page.getByText("The transformation was applied to the local demo.")).toBeVisible();
  await page.getByRole("button", { name: "Undo latest transformation" }).click();
  await expect(page.getByText("The latest completed transformation was reversed.")).toBeVisible();
});

test("dashboard navigation builds an interactive chart", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Skip tour" }).click();
  await openSection(page, "Dashboards");
  await page.getByRole("button", { name: "Build chart" }).click();
  await expect(page.getByRole("img", { name: "Generated data chart" })).toBeVisible();
});
