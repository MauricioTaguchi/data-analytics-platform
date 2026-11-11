export type DataRow = Record<string, string | number | boolean | null>;
export type DataOperation = "drop_duplicates" | "fill_nulls" | "rename_columns" | "cast_types" | "drop_columns";

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

export function exportCsvText(rows: DataRow[], columns: string[]): string {
  const quote = (value: string) => `"${value.replace(/"/g, '""')}"`;
  const encode = (value: DataRow[string] | undefined): string => {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return quote(value);
    if (typeof value === "number" && !Number.isFinite(value)) {
      throw new Error("CSV export requires finite numbers. Correct invalid numeric values first.");
    }
    return String(value);
  };
  return `${[columns.map(quote).join(","), ...rows.map((row) => columns.map((column) => encode(row[column])).join(","))].join("\r\n")}\r\n`;
}

type CsvField = { text: string; quoted: boolean };

export function parseCsvText(text: string): DataRow[] {
  const source = text.replace(/^\uFEFF/, "");
  const records: CsvField[][] = [];
  let record: CsvField[] = [];
  let field = "";
  let quoted = false;
  let wasQuoted = false;
  let closedQuote = false;
  const finishField = () => {
    record.push({ text: wasQuoted ? field : field.trim(), quoted: wasQuoted });
    field = "";
    wasQuoted = false;
    closedQuote = false;
  };
  const finishRecord = (lineBreak = false) => {
    const hasContent = record.length > 0 || field.trim() !== "" || wasQuoted
      || (lineBreak && records[0]?.length === 1);
    finishField();
    if (hasContent) records.push(record);
    record = [];
  };
  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    const nextCharacter = source[index + 1];
    if (character === '"' && quoted && nextCharacter === '"') {
      field += '"'; index += 1;
    } else if (character === '"' && quoted) {
      quoted = false;
      closedQuote = true;
    } else if (character === '"' && field === "" && !closedQuote) {
      quoted = true;
      wasQuoted = true;
    } else if (character === "," && !quoted) finishField();
    else if ((character === "\n" || character === "\r") && !quoted) {
      if (character === "\r" && nextCharacter === "\n") index += 1;
      finishRecord(true);
    } else {
      if (closedQuote || (character === '"' && !quoted)) {
        throw new Error("Invalid CSV quoting. Use double quotes around fields and double any quotes inside them.");
      }
      field += character;
    }
  }
  if (quoted) throw new Error("Invalid CSV quoting. A quoted field was not closed.");
  finishRecord();
  const [header, ...rows] = records;
  if (!header?.length || rows.length === 0) throw new Error("The CSV file does not contain any data rows.");
  const columns = header.map((cell) => cell.text);
  if (columns.some((column) => column.trim() === "") || new Set(columns).size !== columns.length) {
    throw new Error("CSV column names must be nonempty and unique.");
  }
  for (const [index, values] of rows.entries()) {
    if (values.length !== columns.length) throw new Error(`CSV row ${index + 2} has ${values.length} fields; expected ${columns.length}.`);
  }
  return rows.slice(0, 100).map((values) => Object.fromEntries(columns.map((column, index) => {
    const cell = values[index];
    const value = cell.quoted ? cell.text : cell.text === "" ? null
      : cell.text === "true" ? true : cell.text === "false" ? false : coerceValue(cell.text);
    return [column, value];
  })));
}
