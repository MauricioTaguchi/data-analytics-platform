import { expect, test, type Page } from "@playwright/test";

async function openDashboard(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "Skip tour" }).click();
  const menu = page.getByRole("button", { name: "Open menu", exact: true });
  if (await menu.isVisible()) await menu.click();
  await page.getByRole("navigation", { name: "Workspace", exact: true }).getByRole("button", { name: "Dashboards", exact: true }).click();
}

test("chart values expose the plotted metrics and pairs through a keyboard disclosure", async ({ page }, testInfo) => {
  await openDashboard(page);
  const details = page.locator(".chart-values");
  const summary = details.locator("summary");

  for (const type of ["bar", "line", "pie", "scatter", "histogram"]) {
    await page.getByLabel("Chart type", { exact: true }).selectOption(type);
    await page.getByRole("button", { name: "Build chart", exact: true }).click();
    await expect(details).not.toHaveAttribute("open", "");
    await summary.focus();
    await expect(summary).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(details).toHaveAttribute("open", "");
    const table = details.getByRole("table");
    await expect(table).toBeVisible();
    await expect(page.locator(".chart-canvas").getByRole("img")).toHaveAccessibleDescription(/chart values:/);

    if (type === "scatter") {
      await expect(table.getByRole("columnheader")).toHaveText(["X: order_id", "Y: total"]);
      await expect(table.getByRole("row")).toHaveCount(6);
      await expect(table.getByRole("row", { name: "100005 1299", exact: true })).toHaveCount(2);
      await expect(table.getByRole("row", { name: "100001 5299", exact: true })).toBeVisible();
      await expect(page.getByText("1 rows without a finite numeric X/Y pair were excluded.", { exact: true })).toBeVisible();
    } else if (type === "histogram") {
      await expect(table.getByRole("columnheader")).toHaveText(["Interval of total", "Count of recorded values"]);
      const counts = await table.locator("tbody td").allTextContents();
      expect(counts.map(Number).reduce((sum, count) => sum + count, 0)).toBe(5);
    } else {
      await expect(table.getByRole("rowheader", { name: "Computers", exact: true })).toBeVisible();
      await expect(table.getByRole("cell", { name: "5998", exact: true })).toBeVisible();
      await expect(table.getByRole("cell", { name: "3497.9", exact: true })).toBeVisible();
      if (type === "pie") {
        await expect(table.getByRole("columnheader")).toHaveText(["category", "sum of total", "Share of total"]);
        await expect(table.getByRole("row", { name: "Total 9495.9 100%", exact: true })).toBeVisible();
      } else {
        await expect(table.getByRole("columnheader")).toHaveText(["category", "sum of total"]);
        await expect(table.getByRole("rowheader", { name: "Total", exact: true })).toHaveCount(0);
      }
    }

    if (type === "bar") await page.locator(".chart-canvas").screenshot({ path: testInfo.outputPath("chart-values.png") });
    await summary.focus();
    await page.keyboard.press("Space");
    await expect(details).not.toHaveAttribute("open", "");
    await expect(summary).toBeFocused();
    await expect(table).toBeHidden();
  }
});

test("expanded chart values reflow at 320 CSS pixels and with emulated 200 percent zoom", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 320, height: 900 });
  await openDashboard(page);
  await page.getByRole("button", { name: "Build chart", exact: true }).click();
  const details = page.locator(".chart-values");
  await details.locator("summary").click();
  const table = details.getByRole("table");
  await expect(table).toBeVisible();

  async function expectTableReflow() {
    const bounds = await table.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(page.viewportSize()!.width + 1);
    expect(await table.evaluate((element) => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
    await expect(table.getByRole("cell", { name: "3497.9", exact: true })).toBeVisible();
  }

  await expectTableReflow();
  await page.screenshot({ path: testInfo.outputPath("chart-values-320px.png"), fullPage: true });
  // CSS zoom exercises magnification/reflow; it is not a physical-device or assistive-technology test.
  await page.setViewportSize({ width: 640, height: 1000 });
  await page.evaluate(() => { document.documentElement.style.zoom = "2"; });
  await expectTableReflow();
  await details.locator("summary").focus();
  await page.keyboard.press("Space");
  await expect(details).not.toHaveAttribute("open", "");
  await page.keyboard.press("Enter");
  await expect(table).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("chart-values-zoom-200.png"), fullPage: true });
});
