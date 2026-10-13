import { describe, expect, it } from "vitest";
import { applyLocalParameters, buildOperationParameters, DEMO_OPERATIONS, emptyOperationDraft, parameterShape, typedScalar, type OperationDefinition } from "./operation-contracts";
const definition = (operation: string) => DEMO_OPERATIONS.find((item) => item.operation === operation)!;
const draft = emptyOperationDraft;

describe("versioned operation forms", () => {
  it("keeps numeric-looking identifiers as text unless numeric input is explicit", () => {
    expect(typedScalar("001", "text")).toBe("001");
    expect(typedScalar("001", "number")).toBe(1);
    expect(typedScalar("false", "boolean")).toBe(false);
    expect(() => typedScalar("", "number")).toThrow("finite");
    expect(() => typedScalar("Infinity", "number")).toThrow("finite");
    expect(() => typedScalar("yes", "boolean")).toThrow("true or false");
  });
  it("sends multi-column scalar maps without changing their declared types", () => {
    expect(buildOperationParameters(definition("fill_nulls"), { ...draft(), entries: [{ column: "id", value: "001", type: "text" }, { column: "active", value: "false", type: "boolean" }] }, ["id", "active"])).toEqual({ values: { id: "001", active: false } });
  });
  it("rejects duplicate sources, unavailable fields and rename collisions", () => {
    expect(() => buildOperationParameters(definition("rename_columns"), { ...draft(), entries: [{ column: "id", value: "active", type: "text" }] }, ["id", "active"])).toThrow("duplicate column");
    expect(() => buildOperationParameters(definition("fill_nulls"), { ...draft(), entries: [{ column: "missing", value: "", type: "text" }] }, ["id"])).toThrow("available column");
    expect(() => buildOperationParameters(definition("fill_nulls"), { ...draft(), entries: [{ column: "id", value: "", type: "text" }, { column: "id", value: "", type: "text" }] }, ["id"])).toThrow("only once");
  });
  it("handles literal special column names without prototype mutations", () => {
    const parameters = buildOperationParameters(definition("rename_columns"), { ...draft(), entries: [{ column: "__proto__", value: "constructor", type: "text" }] }, ["__proto__"]);
    expect(Object.keys(parameters.mapping as object)).toEqual(["__proto__"]);
    expect(applyLocalParameters([JSON.parse('{"__proto__":"literal"}')], "rename_columns", parameters)).toEqual([{ constructor: "literal" }]);
  });
  it("rejects removing every column and removes only selected fields", () => {
    expect(() => buildOperationParameters(definition("drop_columns"), { ...draft(), columns: ["id"] }, ["id"])).toThrow("at least one");
    expect(applyLocalParameters([{ id: 1, total: 2 }], "drop_columns", { columns: ["total"] })).toEqual([{ id: 1 }]);
  });
  it("uses cast types supplied by a live catalog without a frontend type allowlist", () => {
    const remote: OperationDefinition = { ...definition("cast_types"), parameters_schema: { type: "object", properties: { mapping: { type: "object", additionalProperties: { enum: ["datetime64[ns]", "Int64"] } } } } };
    expect(buildOperationParameters(remote, { ...draft(), entries: [{ column: "date", value: "datetime64[ns]", type: "text" }] }, ["date"])).toEqual({ mapping: { date: "datetime64[ns]" } });
    expect(() => buildOperationParameters(remote, { ...draft(), entries: [{ column: "date", value: "unsupported", type: "text" }] }, ["date"])).toThrow("supported");
  });
  it("exposes a JSON editor for future complex contracts and rejects non-object input", () => {
    const remote = { ...definition("fill_nulls"), parameters_schema: { type: "object", properties: { expression: { type: "string" }, count: { type: "integer" } } } };
    expect(parameterShape(remote).kind).toBe("json");
    expect(buildOperationParameters(remote, { ...draft(), json: '{"expression":"a + b","count":2}' }, [])).toEqual({ expression: "a + b", count: 2 });
    expect(() => buildOperationParameters(remote, { ...draft(), json: "[]" }, [])).toThrow("JSON object");
  });
  it("preserves nulls and rejects unsafe local casts", () => {
    expect(applyLocalParameters([{ value: "false" }, { value: null }], "cast_types", { mapping: { value: "bool" } })).toEqual([{ value: false }, { value: null }]);
    expect(() => applyLocalParameters([{ value: "no" }], "cast_types", { mapping: { value: "float64" } })).toThrow("safely");
    expect(() => applyLocalParameters([{ value: "9007199254740993" }], "cast_types", { mapping: { value: "int64" } })).toThrow("safely");
    expect(() => applyLocalParameters([{ value: "maybe" }], "cast_types", { mapping: { value: "bool" } })).toThrow("boolean");
    expect(() => applyLocalParameters([], "server_only", {})).toThrow("live API");
  });
});
