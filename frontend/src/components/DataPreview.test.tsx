import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DataPreview, inferPreviewType, previewPage } from "./DataPreview";

describe("loaded data preview", () => {
  const rows = Array.from({ length: 23 }, (_, index) => ({ item: `row-${index + 1}` }));

  it("renders ten loaded rows and reports their actual preview range", () => {
    const markup = renderToStaticMarkup(<DataPreview rows={rows} columns={["item"]} visibleColumns={["item"]} search="" onSearch={() => undefined} />);
    expect(markup).toContain("1–10 of 23 preview rows");
    expect(markup).toContain("row-10");
    expect(markup).not.toContain("row-11");
    expect(markup).toContain('aria-label="Previous preview page" disabled=""');
    expect(markup).toContain('aria-label="Next preview page"');
  });

  it("makes every loaded row reachable, including the final partial page", () => {
    expect(previewPage(rows, 2).rows.map((row) => row.item)).toEqual(
      Array.from({ length: 10 }, (_, index) => `row-${index + 11}`),
    );
    expect(previewPage(rows, 3)).toMatchObject({ page: 3, pageCount: 3, offset: 20 });
    expect(previewPage(rows, 3).rows).toEqual(rows.slice(20));
    expect(previewPage(rows.slice(0, 6), 3).page).toBe(1);
    expect(previewPage([], 1).rows).toEqual([]);
  });

  it("infers types across the preview without treating null as an object", () => {
    expect(inferPreviewType([{ value: null }, { value: 4 }], "value")).toBe("number");
    expect(inferPreviewType([{ value: null }], "value")).toBe("empty");
    expect(inferPreviewType([{ value: "4" }, { value: 4 }], "value")).toBe("mixed");
    expect(inferPreviewType([{ value: "2026-10-13" }], "value")).toBe("text");
  });

  it("preserves small values, distinct decimals and numeric identifiers in rendered cells", () => {
    const values = [0.0001, 2.001, 2.002, 100001].map((value) => ({ value }));
    const markup = renderToStaticMarkup(<DataPreview rows={values} columns={["value"]} visibleColumns={["value"]} search="" onSearch={() => undefined} />);
    for (const value of ["0.0001", "2.001", "2.002", "100001"]) {
      expect(markup).toContain(`<td>${value}</td>`);
    }
    expect(markup).not.toContain("100,001");
  });

  it("shows a recoverable empty state when no columns match", () => {
    const markup = renderToStaticMarkup(<DataPreview rows={rows} columns={["item"]} visibleColumns={[]} search="absent" onSearch={() => undefined} />);
    expect(markup).toContain("No matching columns");
    expect(markup).toContain("Clear search");
    expect(markup).toContain("23 preview rows loaded");
    expect(markup).not.toContain("<table");
  });
});
