import { describe, expect, it } from "vitest";
import { applyLocalOperation, buildApiParameters, calculateQuality, exportCsvText, parseCsvText } from "./data-utils";

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

  it("exports RFC-style escaped headers and fields with CRLF records", () => {
    const columns = ['nome,"cliente"', "nota\ncom quebra", "total", "ativo"];
    const source = [{ [columns[0]]: 'José, "Mário"', [columns[1]]: "  linha 1\nlinha 2\rfinal  ", total: 12.5, ativo: true }];
    const csv = exportCsvText(source, columns);

    expect(csv).toBe('"nome,""cliente""","nota\ncom quebra","total","ativo"\r\n"José, ""Mário""","  linha 1\nlinha 2\rfinal  ",12.5,true\r\n');
    expect(csv).not.toContain('\\"');
    expect(parseCsvText(csv)).toEqual(source);
  });

  it("round-trips empty strings, nulls, numeric-looking strings and booleans", () => {
    const source = [
      { id: "001", text: "", optional: null, enabled: false },
      { id: "false", text: "\r\n", optional: "  preserved  ", enabled: true },
    ];
    expect(parseCsvText(exportCsvText(source, ["id", "text", "optional", "enabled"]))).toEqual(source);
    expect(parseCsvText(exportCsvText([{ value: null }, { value: "" }, { value: null }], ["value"]))).toEqual([{ value: null }, { value: "" }, { value: null }]);
  });

  it("preserves requested column order and empty cells", () => {
    const csv = exportCsvText([{ a: 4, b: null, c: "☕" }, { a: null, b: null, c: null }], ["c", "b", "a"]);
    expect(csv).toBe('"c","b","a"\r\n"☕",,4\r\n,,\r\n');
    expect(parseCsvText(csv)).toEqual([{ c: "☕", b: null, a: 4 }, { c: null, b: null, a: null }]);
  });

  it("accepts a UTF-8 BOM and CRLF CSV input", () => {
    expect(parseCsvText('\uFEFFcustomer,total\r\n"Ana",12\r\n')).toEqual([{ customer: "Ana", total: 12 }]);
  });

  it.each([
    ['a,b\n"unfinished,1', "not closed"],
    ['a,b\n"closed"extra,1', "Invalid CSV quoting"],
    ['a,b\nunquoted"quote,1', "Invalid CSV quoting"],
    ["a,a\n1,2", "nonempty and unique"],
    ["a,\n1,2", "nonempty and unique"],
    ["a,b\n1,2,3", "3 fields; expected 2"],
    ["a,b\n1", "1 fields; expected 2"],
  ])("rejects malformed input instead of silently losing cells: %s", (csv, message) => {
    expect(() => parseCsvText(csv)).toThrow(message);
  });

  it("keeps the local preview bounded while validating every imported row", () => {
    const csv = `value\n${Array.from({ length: 105 }, (_, index) => index).join("\n")}`;
    expect(parseCsvText(csv)).toHaveLength(100);
    expect(() => parseCsvText(`${csv}\n1,2`)).toThrow("expected 1");
  });

  it("refuses to export invalid numeric results as plausible values", () => {
    expect(() => exportCsvText([{ total: Number.NaN }], ["total"])).toThrow("finite numbers");
    expect(() => exportCsvText([{ total: Number.POSITIVE_INFINITY }], ["total"])).toThrow("finite numbers");
  });
});
