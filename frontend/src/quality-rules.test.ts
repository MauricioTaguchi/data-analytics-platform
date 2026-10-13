import { describe, expect, it } from "vitest";
import { buildQualityRules, newQualityRule, qualityRuleDraft } from "./quality-rules";

describe("quality rule contracts", () => {
  it("serializes nullable typed accepted values without coercion", () => {
    const rule = { ...newQualityRule("accepted", "status"), kind: "accepted_values" as const, values: '["001",1,true,null]' };
    expect(buildQualityRules([rule])).toEqual([{ id: "accepted", kind: "accepted_values", column: "status", values: ["001", 1, true, null] }]);
  });
  it("rejects nested or empty accepted values", () => {
    expect(() => buildQualityRules([{ ...newQualityRule("accepted", "status"), kind: "accepted_values", values: "[1e999]" }])).toThrow("finite");
    for (const values of ['[{"unexpected":true}]', "[]", "null"]) expect(() => buildQualityRules([{ ...newQualityRule("accepted", "status"), kind: "accepted_values", values }])).toThrow("non-empty JSON array");
  });
  it("validates range boundaries while preserving a zero bound", () => {
    expect(buildQualityRules([{ ...newQualityRule("range", "total"), kind: "range", min: "0" }])[0]).toEqual({ id: "range", kind: "range", column: "total", min: 0 });
    expect(() => buildQualityRules([{ ...newQualityRule("range", "total"), kind: "range" }])).toThrow("minimum or maximum");
    expect(() => buildQualityRules([{ ...newQualityRule("range", "total"), kind: "range", min: "3", max: "1" }])).toThrow("cannot exceed");
    expect(() => buildQualityRules([{ ...newQualityRule("range", "total"), kind: "range", max: "Infinity" }])).toThrow("finite");
  });
  it("round trips stored schema expectations without inventing inferred types", () => {
    const rule = { id: "schema", kind: "schema" as const, expected_schema: { id: "Int64", flag: "boolean" }, allow_extra_columns: true };
    expect(buildQualityRules([qualityRuleDraft(rule)])).toEqual([rule]);
    expect(() => buildQualityRules([{ ...newQualityRule("schema", ""), kind: "schema", schema: '{"id":1}' }])).toThrow("dtype strings");
  });
  it("requires at least one rule and a column for column-scoped rules", () => {
    expect(() => buildQualityRules([])).toThrow("1 and 50");
    expect(() => buildQualityRules([newQualityRule("required", "")])).toThrow("Select a column");
    expect(buildQualityRules([newQualityRule("required", "id")])).toEqual([{ id: "required", kind: "not_null", column: "id" }]);
  });
});

it("restores nullable server range bounds as empty optional fields", () => {
  const saved = qualityRuleDraft({ id: "range", kind: "range", column: "total", min: null, max: 100 });
  expect(saved.min).toBe("");
  expect(buildQualityRules([saved])[0]).toEqual({ id: "range", kind: "range", column: "total", max: 100 });
});
