import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { buildLocalChartData, ChartVisual, InteractiveRequestFence } from "./DashboardWorkspace";

const rows = [
  { category: "Computers", total: 200 },
  { category: "Furniture", total: 75 },
  { category: "Computers", total: 50 },
];

describe("DashboardWorkspace", () => {
  it("invalidates an in-flight chart build when the project or session changes", () => {
    const fence = new InteractiveRequestFence();
    const buildRequest = fence.begin();

    expect(fence.isCurrent(buildRequest)).toBe(true);
    expect(fence.invalidate()).toBe(true);
    expect(buildRequest.controller.signal.aborted).toBe(true);
    expect(fence.isCurrent(buildRequest)).toBe(false);

    const nextProjectRequest = fence.begin();
    expect(nextProjectRequest.epoch).toBeGreaterThan(buildRequest.epoch);
    expect(fence.isCurrent(nextProjectRequest)).toBe(true);
  });

  it("keeps a superseded request from finishing the active chart request", () => {
    const fence = new InteractiveRequestFence();
    const staleRequest = fence.begin();
    const activeRequest = fence.begin();

    expect(staleRequest.controller.signal.aborted).toBe(true);
    expect(fence.finish(staleRequest)).toBe(false);
    expect(fence.isCurrent(activeRequest)).toBe(true);
    expect(fence.finish(activeRequest)).toBe(true);
    expect(fence.isCurrent(activeRequest)).toBe(false);
  });

  it("aggregates local chart data in a single pass", () => {
    const result = buildLocalChartData(rows, "category", "total", "sum");

    expect(result.labels).toEqual(["Computers", "Furniture"]);
    expect(result.values).toEqual([250, 75]);
    expect(buildLocalChartData(rows, "category", "total", "mean").values).toEqual([125, 75]);
  });

  it("renders aggregated data as an accessible chart", () => {
    const markup = renderToStaticMarkup(
      <ChartVisual data={buildLocalChartData(rows, "category", "total", "sum")} />,
    );

    expect(markup).toContain('role="img"');
    expect(markup).toContain('aria-label="Generated data chart"');
    expect(markup).toContain("Computers");
    expect(markup).toContain("250");
  });

  it("renders distinct accessible line and pie visualizations", () => {
    const data = buildLocalChartData(rows, "category", "total", "sum");
    const line = renderToStaticMarkup(<ChartVisual data={data} type="line" />);
    const pie = renderToStaticMarkup(<ChartVisual data={data} type="pie" />);

    expect(line).toContain('aria-label="Generated line chart"');
    expect(line).toContain("polyline");
    expect(pie).toContain('aria-label="Generated pie chart"');
    expect(pie).toContain("76.9%");
  });

  it("ignores missing measures and builds a real local histogram", () => {
    const valuesWithMissing = [...rows, { category: "Computers", total: null }];

    expect(buildLocalChartData(valuesWithMissing, "category", "total", "count").values).toEqual([2, 1]);
    expect(buildLocalChartData(rows, "category", "total", "min", "kpi").values).toEqual([50]);

    const histogram = buildLocalChartData(rows, "category", "total", "count", "histogram");
    expect(histogram.labels).toHaveLength(10);
    expect(histogram.values.map(Number).reduce((total, value) => total + value, 0)).toBe(rows.length);
  });

  it("matches backend null-group semantics for every aggregation", () => {
    const rowsWithEmptyGroup = [...rows, { category: "Services", total: null }];

    expect(buildLocalChartData(rowsWithEmptyGroup, "category", "total", "sum")).toMatchObject({
      labels: ["Computers", "Furniture", "Services"],
      values: [250, 75, 0],
    });
    expect(buildLocalChartData(rowsWithEmptyGroup, "category", "total", "count")).toMatchObject({
      labels: ["Computers", "Furniture", "Services"],
      values: [2, 1, 0],
    });
    for (const aggregation of ["mean", "min", "max"] as const) {
      expect(buildLocalChartData(rowsWithEmptyGroup, "category", "total", aggregation).labels).toEqual(["Computers", "Furniture"]);
    }
  });

  it("renders an unavailable KPI instead of inventing a zero", () => {
    const data = buildLocalChartData(
      [{ category: "Services", total: null }],
      "category",
      "total",
      "mean",
      "kpi",
    );
    const markup = renderToStaticMarkup(<ChartVisual data={data} type="kpi" />);

    expect(data.values).toEqual([null]);
    expect(markup).toContain("—");
    expect(markup).not.toContain(">0<");
  });
});
