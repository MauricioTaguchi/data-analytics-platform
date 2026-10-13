import type { DataRow } from "./data-utils";

export type JsonSchema = { type?: string; title?: string; description?: string; enum?: string[]; anyOf?: JsonSchema[]; properties?: Record<string, JsonSchema>; additionalProperties?: boolean | JsonSchema; items?: JsonSchema; required?: string[] };
export type OperationDefinition = { operation: string; semantic_version: string; parameters_schema: JsonSchema; engines: Record<string, string[]>; invariants: Record<string, boolean> };
export type ScalarType = "text" | "number" | "boolean";
export type ParameterEntry = { column: string; value: string; type: ScalarType };
export type OperationDraft = { entries: ParameterEntry[]; columns: string[]; json: string };

const scalarSchema: JsonSchema = { anyOf: [{ type: "string" }, { type: "integer" }, { type: "number" }, { type: "boolean" }] };
const mapping = (valueSchema: JsonSchema): JsonSchema => ({ type: "object", properties: { mapping: { type: "object", additionalProperties: valueSchema } }, additionalProperties: false });

// The demo explicitly declares its smaller local runtime. Live contracts always come from the API.
export const DEMO_OPERATIONS: OperationDefinition[] = [
  { operation: "drop_duplicates", parameters_schema: { type: "object", properties: {} } },
  { operation: "drop_columns", parameters_schema: { type: "object", properties: { columns: { type: "array", items: { type: "string" } } } } },
  { operation: "rename_columns", parameters_schema: mapping({ type: "string" }) },
  { operation: "fill_nulls", parameters_schema: { type: "object", properties: { values: { type: "object", additionalProperties: scalarSchema } } } },
  { operation: "cast_types", parameters_schema: mapping({ type: "string", enum: ["string", "float64", "int64", "bool"] }) },
].map((item) => ({ ...item, semantic_version: "1.0.0", engines: { browser: ["local preview"] }, invariants: {} }));

export function emptyOperationDraft(): OperationDraft { return { entries: [{ column: "", value: "", type: "text" }], columns: [], json: "{}" }; }

export function parameterShape(definition: OperationDefinition) {
  const properties = definition.parameters_schema.properties;
  const fields = Object.entries(properties || {});
  if (properties && fields.length === 0) return { kind: "empty" as const };
  if (fields.length === 1) {
    const [name, schema] = fields[0];
    if (schema.type === "array" && schema.items?.type === "string") return { kind: "columns" as const, name, schema };
    if (schema.type === "object" && typeof schema.additionalProperties === "object") {
      const valueSchema = schema.additionalProperties;
      if (valueSchema.enum || valueSchema.type === "string" || valueSchema.anyOf?.every((item) => ["string", "integer", "number", "boolean"].includes(item.type || ""))) {
        return { kind: "mapping" as const, name, schema: valueSchema };
      }
    }
  }
  return { kind: "json" as const };
}

export function parseFiniteJson(text: string): unknown {
  return JSON.parse(text, (_key, value: unknown) => {
    if (typeof value === "number" && !Number.isFinite(value)) throw new Error("JSON numbers must be finite.");
    return value;
  });
}

export function typedScalar(value: string, type: ScalarType): string | number | boolean {
  if (type === "boolean") {
    if (!["true", "false"].includes(value)) throw new Error("Choose true or false for a boolean value.");
    return value === "true";
  }
  if (type === "number") {
    if (value.trim() === "" || !Number.isFinite(Number(value))) throw new Error("Enter a finite numeric value.");
    return Number(value);
  }
  return value;
}

export function buildOperationParameters(definition: OperationDefinition, draft: OperationDraft, columns: string[]): Record<string, unknown> {
  const shape = parameterShape(definition);
  if (shape.kind === "empty") return {};
  if (shape.kind === "json") {
    const value: unknown = parseFiniteJson(draft.json);
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Parameters must be a JSON object matching the displayed contract.");
    return value as Record<string, unknown>;
  }
  if (shape.kind === "columns") {
    if (!draft.columns.length) throw new Error("Select at least one column.");
    if (draft.columns.some((column) => !columns.includes(column))) throw new Error("A selected column is no longer available.");
    if (definition.operation === "drop_columns" && draft.columns.length === columns.length) throw new Error("Keep at least one column in the dataset.");
    return { [shape.name]: draft.columns };
  }
  const entries = draft.entries.map((entry) => {
    const column = entry.column || columns[0];
    if (!column || !columns.includes(column)) throw new Error("Select an available column for every parameter.");
    const value = shape.schema.enum ? entry.value || shape.schema.enum[0]
      : shape.schema.anyOf ? typedScalar(entry.value, entry.type) : entry.value;
    if (shape.schema.enum && !shape.schema.enum.includes(String(value))) throw new Error("Choose a type supported by the operation contract.");
    if (definition.operation === "rename_columns" && (typeof value !== "string" || !value.trim() || value.includes("\0"))) throw new Error("Enter a non-empty column name without null bytes.");
    return [column, value] as const;
  });
  if (!entries.length) throw new Error("Add at least one column parameter.");
  if (new Set(entries.map(([column]) => column)).size !== entries.length) throw new Error("Choose each source column only once.");
  if (definition.operation === "rename_columns") {
    const names = columns.map((column) => entries.find(([source]) => source === column)?.[1] ?? column);
    if (new Set(names).size !== names.length) throw new Error("Renaming would create duplicate column names.");
  }
  return { [shape.name]: Object.fromEntries(entries) };
}

export function applyLocalParameters(rows: DataRow[], operation: string, parameters: Record<string, unknown>): DataRow[] {
  if (!DEMO_OPERATIONS.some((item) => item.operation === operation)) throw new Error("This operation requires the live API.");
  if (operation === "drop_duplicates") return Array.from(new Map(rows.map((row) => [JSON.stringify(row), { ...row }])).values());
  if (operation === "drop_columns") return rows.map((row) => Object.fromEntries(Object.entries(row).filter(([key]) => !(parameters.columns as string[]).includes(key))));
  const mapping = (parameters.mapping || parameters.values || {}) as Record<string, string | number | boolean>;
  return rows.map((row) => Object.fromEntries(Object.entries(row).map(([key, value]) => {
    if (!Object.prototype.hasOwnProperty.call(mapping, key)) return [key, value];
    if (operation === "rename_columns") return [String(mapping[key]), value];
    if (operation === "fill_nulls") return [key, value ?? mapping[key]];
    if (operation !== "cast_types") throw new Error("This operation requires the live API.");
    if (value === null) return [key, null];
    const target = mapping[key];
    if (target === "string") return [key, String(value)];
    if (target === "bool") {
      if (![true, false, "true", "false", 1, 0, "1", "0"].includes(value)) throw new Error(`Column ${key} contains a value that cannot be converted to boolean.`);
      return [key, [true, "true", 1, "1"].includes(value)];
    }
    const numeric = typeof value === "string" && !value.trim() ? NaN : Number(value);
    if (!Number.isFinite(numeric) || (target === "int64" && !Number.isSafeInteger(numeric))) throw new Error(`Column ${key} contains a value that cannot be converted safely.`);
    return [key, numeric];
  })));
}
