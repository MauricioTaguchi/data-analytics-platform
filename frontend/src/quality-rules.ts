import { parseFiniteJson } from "./operation-contracts";
import type { QualityRule } from "./api-client";

export type QualityRuleDraft = { id: string; kind: QualityRule["kind"]; column: string; min: string; max: string; values: string; schema: string; allowExtra: boolean };
export function newQualityRule(id: string, column: string): QualityRuleDraft {
  return { id, kind: "not_null", column, min: "", max: "", values: '["active", "inactive"]', schema: '{"order_id":"int64"}', allowExtra: false };
}
export function qualityRuleDraft(rule: QualityRule): QualityRuleDraft {
  return { id: rule.id, kind: rule.kind, column: rule.column || "", min: rule.min == null ? "" : String(rule.min), max: rule.max == null ? "" : String(rule.max), values: JSON.stringify(rule.values || []), schema: JSON.stringify(rule.expected_schema || {}, null, 2), allowExtra: rule.allow_extra_columns || false };
}
export function buildQualityRules(drafts: QualityRuleDraft[]): QualityRule[] {
  if (!drafts.length || drafts.length > 50) throw new Error("A quality contract must contain between 1 and 50 rules.");
  return drafts.map((draft) => {
    const common = { id: draft.id, kind: draft.kind };
    if (draft.kind === "schema") {
      const schema: unknown = JSON.parse(draft.schema);
      if (!schema || typeof schema !== "object" || Array.isArray(schema) || !Object.keys(schema).length || Object.values(schema).some((value) => typeof value !== "string" || !value.trim())) throw new Error("Expected schema must be a JSON object mapping column names to dtype strings.");
      return { ...common, expected_schema: schema as Record<string, string>, allow_extra_columns: draft.allowExtra };
    }
    if (!draft.column) throw new Error("Select a column for every quality rule.");
    if (draft.kind === "range") {
      if (!draft.min.trim() && !draft.max.trim()) throw new Error("A range rule requires a minimum or maximum.");
      const min = draft.min.trim() ? Number(draft.min) : undefined;
      const max = draft.max.trim() ? Number(draft.max) : undefined;
      if ((min !== undefined && !Number.isFinite(min)) || (max !== undefined && !Number.isFinite(max))) throw new Error("Range bounds must be finite numbers.");
      if (min !== undefined && max !== undefined && min > max) throw new Error("The minimum cannot exceed the maximum.");
      return { ...common, column: draft.column, ...(min !== undefined ? { min } : {}), ...(max !== undefined ? { max } : {}) };
    }
    if (draft.kind === "accepted_values") {
      const values: unknown = parseFiniteJson(draft.values);
      if (!Array.isArray(values) || !values.length || values.some((value) => value !== null && !["string", "number", "boolean"].includes(typeof value))) throw new Error("Accepted values must be a non-empty JSON array of text, numbers, booleans, or null.");
      return { ...common, column: draft.column, values };
    }
    return { ...common, column: draft.column };
  });
}
