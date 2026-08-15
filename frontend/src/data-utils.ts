export type DataRow = Record<string, string | number | null>;
export type DataOperation = "drop_duplicates" | "fill_nulls" | "rename_columns" | "cast_types";

export type OperationOptions = { column?: string; value?: string };

function coerceValue(value: string) {
  const numericValue = Number(value);
  return value.trim() !== "" && Number.isFinite(numericValue) ? numericValue : value;
}

export function applyLocalOperation(rows: DataRow[], operation: DataOperation, options: OperationOptions = {}): DataRow[] {
  const copy = rows.map((row) => ({ ...row }));
  if (operation === "drop_duplicates") {
    return Array.from(new Map(copy.map((row) => [JSON.stringify(row), row])).values());
  }
  const column = options.column || Object.keys(copy[0] || {})[0];
  if (!column) return copy;
  if (operation === "fill_nulls") {
    const replacement = coerceValue(options.value || "0");
    return copy.map((row) => ({ ...row, [column]: row[column] ?? replacement }));
  }
  if (operation === "rename_columns") {
    const nextName = options.value?.trim() || `${column}_renamed`;
    return copy.map((row) => Object.fromEntries(
      Object.entries(row).map(([key, value]) => [key === column ? nextName : key, value]),
    ));
  }
  const targetType = options.value || "text";
  return copy.map((row) => {
    const value = row[column];
    if (value === null) return row;
    return { ...row, [column]: targetType === "number" ? Number(value) : String(value) };
  });
}

export function buildApiParameters(operation: DataOperation, options: OperationOptions) {
  const column = options.column || "";
  const value = options.value || "";
  if (operation === "fill_nulls") return { values: { [column]: coerceValue(value || "0") } };
  if (operation === "rename_columns") return { mapping: { [column]: value.trim() || `${column}_renamed` } };
  if (operation === "cast_types") return { mapping: { [column]: value === "number" ? "float64" : "string" } };
  return {};
}

export function calculateQuality(rows: DataRow[]) {
  const duplicateCount = rows.length - new Set(rows.map((row) => JSON.stringify(row))).size;
  const missingCount = rows.flatMap(Object.values).filter((value) => value === null || value === "").length;
  return { duplicateCount, missingCount, score: Math.max(0, Math.round(100 - duplicateCount * 5 - missingCount * 3)) };
}

export function parseCsvText(text: string): DataRow[] {
  const records: string[][] = [];
  let record: string[] = [];
  let field = "";
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    const nextCharacter = text[index + 1];
    if (character === '"' && quoted && nextCharacter === '"') {
      field += '"'; index += 1;
    } else if (character === '"') quoted = !quoted;
    else if (character === "," && !quoted) { record.push(field.trim()); field = ""; }
    else if ((character === "\n" || character === "\r") && !quoted) {
      if (character === "\r" && nextCharacter === "\n") index += 1;
      record.push(field.trim());
      if (record.some(Boolean)) records.push(record);
      record = []; field = "";
    } else field += character;
  }
  record.push(field.trim());
  if (record.some(Boolean)) records.push(record);
  const [header, ...rows] = records;
  if (!header?.length || rows.length === 0) throw new Error("The CSV file does not contain any data rows.");
  return rows.slice(0, 100).map((values) => Object.fromEntries(
    header.map((column, index) => [column, values[index] === "" || values[index] === undefined ? null : coerceValue(values[index])]),
  ));
}
