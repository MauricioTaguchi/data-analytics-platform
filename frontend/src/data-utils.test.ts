import { describe, expect, it } from "vitest";
import { applyLocalOperation, buildApiParameters, calculateQuality, parseCsvText } from "./data-utils";

const rows = [
  { order_id: 1, total: 10 },
  { order_id: 1, total: 10 },
  { order_id: 2, total: null },
];

describe("data workspace utilities", () => {
  it("removes duplicate rows without mutating the input", () => {
    const result = applyLocalOperation(rows, "drop_duplicates");
    expect(result).toHaveLength(2);
    expect(rows).toHaveLength(3);
  });

  it("fills null values in the selected column and recalculates quality", () => {
    const result = applyLocalOperation(rows, "fill_nulls", { column: "total", value: "25" });
    expect(result[2].total).toBe(25);
    expect(calculateQuality(result).missingCount).toBe(0);
  });

  it("creates backend-compatible transformation parameters", () => {
    expect(buildApiParameters("rename_columns", { column: "total", value: "revenue" })).toEqual({
      mapping: { total: "revenue" },
    });
    expect(buildApiParameters("fill_nulls", { column: "total", value: "12" })).toEqual({
      values: { total: 12 },
    });
    expect(buildApiParameters("cast_types", { column: "total", value: "number" })).toEqual({
      mapping: { total: "float64" },
    });
    expect(buildApiParameters("drop_duplicates", {})).toEqual({});
  });

  it("renames columns and casts values without changing nulls", () => {
    const renamed = applyLocalOperation(rows, "rename_columns", { column: "total", value: "revenue" });
    expect(renamed[0]).toEqual({ order_id: 1, revenue: 10 });

    const castToText = applyLocalOperation(rows, "cast_types", { column: "order_id", value: "text" });
    expect(castToText[0].order_id).toBe("1");
    expect(applyLocalOperation(rows, "cast_types", { column: "total", value: "number" })[2].total).toBeNull();
  });

  it("parses quoted CSV fields without splitting embedded commas", () => {
    const result = parseCsvText('customer,product,total\n"Silva, Ana","24-inch Monitor",699');
    expect(result[0]).toEqual({ customer: "Silva, Ana", product: "24-inch Monitor", total: 699 });

    const escapedQuote = parseCsvText('customer,note\nAna,"Said ""hello"""');
    expect(escapedQuote[0]).toEqual({ customer: "Ana", note: 'Said "hello"' });
  });

  it("rejects empty CSV files with an actionable error", () => {
    expect(() => parseCsvText("customer,total\n")).toThrow("does not contain any data rows");
  });
});
