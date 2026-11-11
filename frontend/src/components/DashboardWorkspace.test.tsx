import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { buildLocalChartData, ChartMetricSummary, ChartVisual, InteractiveRequestFence } from "./DashboardWorkspace";

const rows = [
  { category: "Computers", total: 200 },
  { category: "Furniture", total: 75 },
  { category: "Computers", total: 50 },
];

describe("DashboardWorkspace", () => {
  it.each(["mean", "min", "max"] as const)("rejects non-additive %s pie slices in calculation and display", (aggregation) => {
    expect(() => buildLocalChartData(rows, "category", "total", aggregation, "pie")).toThrow("sum or count only");
    const markup = renderToStaticMarkup(<ChartVisual data={{ labels: ["A", "B"], values: [10, 100], rows: [], aggregation }} type="pie" />);
    expect(markup).toContain("Choose an additive measure");
    expect(markup).not.toContain("conic-gradient");
  });

  it("explains category means using the original row denominator and source unit", () => {
    const observations = [{ x: "A", y: 10 }, { x: "A", y: 10 }, { x: "A", y: 10 }, { x: "A", y: 10 }, { x: "B", y: 100 }, { x: "B", y: null }];
    const categories = buildLocalChartData(observations, "x", "y", "mean");
    const overall = buildLocalChartData(observations, "x", "y", "mean", "kpi");
    expect(categories.values).toEqual([10, 100]);
    expect(overall.values).toEqual([28]);
    expect(categories).toMatchObject({ source_row_count: 6, valid_value_count: 5, omitted_count: 1 });
    const markup = renderToStaticMarkup(<ChartMetricSummary data={categories} type="bar" />);
    expect(markup).toContain("calculated separately for each x");
    expect(markup).toContain("source unit of y");
    expect(markup).toContain("Each usable source row has equal weight");
    expect(markup).toContain("5 recorded values used");
    expect(markup).toContain("1 missing or non-finite measure values excluded");
  });

  it("counts non-null text cells and duplicates consistently with the API", () => {
    const observations = [{ x: "A", y: "same" }, { x: "A", y: "same" }, { x: "A", y: "" }, { x: "A", y: null }];
    const data = buildLocalChartData(observations, "x", "y", "count");
    expect(data.values).toEqual([3]);
    expect(buildLocalChartData(observations, "x", "y", "count", "kpi").values).toEqual([3]);
    expect(data).toMatchObject({ valid_value_count: 3, omitted_count: 1 });
    expect(renderToStaticMarkup(<ChartMetricSummary data={data} type="bar" />)).toContain("duplicates count separately");
  });

  it("distinguishes no matching rows from an all-missing measure", () => {
    const empty = buildLocalChartData([], "x", "y", "mean", "kpi");
    expect(renderToStaticMarkup(<ChartVisual data={empty} type="kpi" />)).toContain("No matching rows");
    const missing = buildLocalChartData([{ x: "A", y: null }], "x", "y", "sum", "kpi");
    expect(missing.values).toEqual([0]);
    expect(renderToStaticMarkup(<ChartMetricSummary data={missing} type="kpi" />)).toContain("A zero sum or count does not mean a measured value of zero");
  });

  it("reports non-finite numeric omissions and rejects them for a pie", () => {
    const observations = [{ x: "A", y: 10 }, { x: "A", y: Number.POSITIVE_INFINITY }, { x: "A", y: null }];
    expect(buildLocalChartData(observations, "x", "y", "mean")).toMatchObject({ values: [10], source_row_count: 3, valid_value_count: 1, omitted_count: 2 });
    expect(() => buildLocalChartData(observations, "x", "y", "sum", "pie")).toThrow("finite, non-negative");
  });

  it("preserves boolean KPI reductions without accepting boolean scatter coordinates", () => {
    const flags = [{ x: 1, flag: true }, { x: 2, flag: false }, { x: 3, flag: null }];
    expect(buildLocalChartData(flags, "x", "flag", "sum", "kpi").values).toEqual([1]);
    expect(buildLocalChartData(flags, "x", "flag", "count", "kpi").values).toEqual([2]);
    expect(buildLocalChartData(flags, "x", "flag", "mean", "kpi").values).toEqual([0.5]);
    expect(buildLocalChartData(flags, "x", "flag", "sum", "scatter")).toMatchObject({ labels: [], values: [], omitted_count: 3 });
  });
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

  it("includes every pie category in its total and combines the remainder", () => {
    const data = { labels: Array.from({ length: 9 }, (_, index) => `Category ${index + 1}`), values: Array(9).fill(1), rows: [] };
    const markup = renderToStaticMarkup(<ChartVisual data={data} type="pie" />);

    expect(markup).toContain("All 9 categories are included");
    expect(markup).toContain("Other (2 categories)");
    expect(markup).toContain(">9</span>");
    expect(markup).toContain("22.2%");
    expect(markup.match(/<strong>11\.1%<\/strong>/g)).toHaveLength(7);
  });

  it("preserves the complete total when the API already combined pie categories", () => {
    const data = {
      labels: [...Array.from({ length: 19 }, (_, index) => `Category ${index + 1}`), "Other"],
      values: [...Array(19).fill(1), 81], rows: [], total_count: 100, returned_count: 20, grouped_count: 81, truncated: false,
    };
    const markup = renderToStaticMarkup(<ChartVisual data={data} type="pie" />);

    expect(markup).toContain("All 100 categories are included");
    expect(markup).toContain("Other (93 categories)");
    expect(markup).toContain(">100</span>");
    expect(markup).toContain("93.0%");
  });

  it.each([-1, null, "", "invalid", Number.POSITIVE_INFINITY])("does not disguise invalid pie values as zero: %s", (value) => {
    const markup = renderToStaticMarkup(<ChartVisual data={{ labels: ["A", "B"], values: [10, value], rows: [] }} type="pie" />);
    expect(markup).toContain("Pie chart unavailable");
    expect(markup).not.toContain("conic-gradient");
  });

  it("does not invent a pie distribution from zero or truncated data", () => {
    expect(renderToStaticMarkup(<ChartVisual data={{ labels: ["A"], values: [0], rows: [] }} type="pie" />)).toContain("No positive total");
    expect(renderToStaticMarkup(<ChartVisual data={{ labels: ["A"], values: [10], rows: [], truncated: true, total_count: 2 }} type="pie" />)).toContain("Incomplete pie data");
  });

  it("builds scatter data from original paired rows without aggregating duplicate X values", () => {
    const data = buildLocalChartData([
      { x: 0, y: 20 }, { x: 1, y: 30 }, { x: 1, y: 10 }, { x: 100, y: 20 },
      { x: null, y: 40 }, { x: 2, y: null }, { x: "invalid", y: 80 },
    ], "x", "y", "sum", "scatter");

    expect(data.labels).toEqual([0, 1, 1, 100]);
    expect(data.values).toEqual([20, 30, 10, 20]);
    expect(data).toMatchObject({ total_count: 4, returned_count: 4, omitted_count: 3, truncated: false });
    const markup = renderToStaticMarkup(<ChartVisual data={data} type="scatter" />);
    const xPositions = [...markup.matchAll(/<circle[^>]*cx="([^"]+)"/g)].map((match) => Number(match[1]));
    expect(xPositions).toEqual([30, 35.4, 35.4, 570]);
    expect(markup).toContain("X: 1; Y: 10");
    expect(markup).toContain("3 rows without a finite numeric X/Y pair were excluded");
    expect(markup).not.toContain("polyline");
  });

  it("excludes invalid scatter pairs together so X and Y never shift", () => {
    const markup = renderToStaticMarkup(<ChartVisual data={{ labels: [0, "bad", 100], values: [10, 900, 20], rows: [] }} type="scatter" />);
    expect(markup.match(/<circle /g)).toHaveLength(2);
    expect(markup).toContain("X: 100; Y: 20");
    expect(markup).not.toContain("900");
    expect(markup).toContain("1 rows without a finite numeric X/Y pair were excluded");
  });

  it("keeps missing line values as gaps without connecting across them", () => {
    const markup = renderToStaticMarkup(<ChartVisual data={{ labels: ["A", "B", "C"], values: [10, null, 20], rows: [] }} type="line" />);
    expect(markup).toContain("shown as gaps, not zero");
    expect(markup.match(/<circle /g)).toHaveLength(2);
    expect(markup).not.toContain("polyline");
  });

  it("renders signed bars around zero and does not give zero or missing values a fake height", () => {
    const markup = renderToStaticMarkup(<ChartVisual data={{ labels: ["positive", "negative", "zero", "missing"], values: [10, -10, 0, null], rows: [] }} />);
    expect(markup).toContain("Negative values extend below the zero baseline");
    expect(markup).toContain('class="negative" style="position:absolute;top:50%;height:50%');
    expect(markup).toContain("height:0%");
    expect(markup).toContain('class="bar-value">—</div>');
  });

  it("announces every display limit, including API truncation", () => {
    const labels = Array.from({ length: 30 }, (_, index) => index);
    const data = { labels, values: labels, rows: [], total_count: 150, returned_count: 30, truncated: true };
    expect(renderToStaticMarkup(<ChartVisual data={data} />)).toContain("Showing the first 12 of 150 categories");
    expect(renderToStaticMarkup(<ChartVisual data={data} type="line" />)).toContain("Showing the first 24 of 150 categories");
    expect(renderToStaticMarkup(<ChartVisual data={data} type="scatter" />)).toContain("Showing the first 30 of 150 numeric pairs");
    const tableData = { ...data, labels: [], values: [], rows: labels.map((value) => ({ value })) };
    expect(renderToStaticMarkup(<ChartVisual data={tableData} type="table" />)).toContain("Showing the first 20 of 150 rows");
  });

  it("bounds local scatter and table previews with explicit total counts", () => {
    const data = Array.from({ length: 105 }, (_, index) => ({ x: index, y: index * 2 }));
    const scatter = buildLocalChartData(data, "x", "y", "sum", "scatter");
    expect(scatter.labels).toHaveLength(100);
    expect(scatter).toMatchObject({ total_count: 105, returned_count: 100, truncated: true });
    const table = buildLocalChartData(data, "x", "y", "sum", "table");
    expect(table.rows).toHaveLength(100);
    expect(table).toMatchObject({ total_count: 105, returned_count: 100, truncated: true });
  });
});
