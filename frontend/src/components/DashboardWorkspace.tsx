import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { createChart, createDashboard, describeApiError, fetchChartData, isRequestCancelled, listCharts, listDashboards, type Aggregation, type Chart, type ChartData, type ChartType, type Dashboard } from "../api-client";
import type { DataRow } from "../data-utils";

type Props = { live: boolean; projectId: number | null; datasetId: number | null; rows: DataRow[]; columns: string[]; externalBusy: boolean; onNotice: (message: string) => void; onBusyChange: (busy: boolean) => void };

type ChartAccumulator = { count: number; numericCount: number; sum: number; min: number; max: number };
export type InteractiveRequest = Readonly<{ controller: AbortController; epoch: number }>;

export class InteractiveRequestFence {
  private controller: AbortController | null = null;
  private epoch = 0;

  begin(): InteractiveRequest {
    this.controller?.abort();
    const controller = new AbortController();
    this.controller = controller;
    this.epoch += 1;
    return { controller, epoch: this.epoch };
  }

  invalidate() {
    const hadActiveRequest = Boolean(this.controller);
    this.controller?.abort();
    this.controller = null;
    this.epoch += 1;
    return hadActiveRequest;
  }

  isCurrent(request: InteractiveRequest) {
    return this.controller === request.controller
      && this.epoch === request.epoch
      && !request.controller.signal.aborted;
  }

  finish(request: InteractiveRequest) {
    if (!this.isCurrent(request)) return false;
    this.controller = null;
    return true;
  }
}

function numericValuesFor(rows: DataRow[], column: string) {
  return rows.flatMap((row) => {
    const raw = row[column];
    const value = finiteNumber(typeof raw === "boolean" ? Number(raw) : raw);
    return value === null ? [] : [value];
  });
}

function finiteNumber(value: unknown): number | null {
  if (value === null || value === undefined || typeof value === "boolean"
    || (typeof value === "string" && value.trim() === "")) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function localMetric(data: ChartData, rows: DataRow[], xColumn: string, yColumn: string, aggregation: Aggregation, type: ChartType, validCount: number): ChartData {
  return { ...data, aggregation: type === "scatter" || type === "table" ? null : type === "histogram" ? "count" : aggregation,
    value_column: type === "table" ? null : yColumn,
    group_column: type === "kpi" || type === "histogram" || type === "table" ? null : xColumn,
    source_row_count: rows.length, valid_value_count: validCount, omitted_count: rows.length - validCount };
}

function withChartDefinition(data: ChartData, chart: Chart): ChartData {
  const kind = chart.chart_type;
  return { ...data,
    aggregation: data.aggregation === undefined ? kind === "scatter" || kind === "table" ? null
      : kind === "histogram" || (kind === "pie" && !chart.y_column) ? "count" : chart.aggregation || (kind === "kpi" ? "count" : "sum") : data.aggregation,
    value_column: data.value_column === undefined ? kind === "table" ? null : kind === "histogram" ? chart.x_column : chart.y_column || (kind === "kpi" ? chart.x_column : null) : data.value_column,
    group_column: data.group_column === undefined ? kind === "table" || kind === "histogram" || kind === "kpi" ? null : chart.x_column : data.group_column,
  };
}

export function buildLocalChartData(rows: DataRow[], xColumn: string, yColumn: string, aggregation: Aggregation, type: ChartType = "bar"): ChartData {
  if (type === "pie" && aggregation !== "sum" && aggregation !== "count") {
    throw new Error("Pie charts support sum or count only. Use a bar, line or KPI for mean, min or max; these metrics are not additive.");
  }
  if (type === "pie" && aggregation === "sum" && rows.some((row) => {
    const value = row[yColumn];
    return value !== null && value !== undefined && value !== "" && finiteNumber(typeof value === "boolean" ? Number(value) : value) === null;
  })) throw new Error("Pie charts require finite, non-negative category values.");
  if (type === "table") return localMetric({ labels: [], values: [], rows: rows.slice(0, 100), total_count: rows.length, returned_count: Math.min(rows.length, 100), truncated: rows.length > 100 }, rows, xColumn, yColumn, aggregation, type, rows.length);
  if (type === "scatter") {
    const pairs = rows.flatMap((row) => {
      const x = finiteNumber(row[xColumn]);
      const y = finiteNumber(row[yColumn]);
      return x === null || y === null ? [] : [{ x, y }];
    });
    return localMetric({
      labels: pairs.slice(0, 100).map((pair) => pair.x),
      values: pairs.slice(0, 100).map((pair) => pair.y),
      rows: [],
      total_count: pairs.length,
      returned_count: Math.min(pairs.length, 100),
      truncated: pairs.length > 100,
      omitted_count: rows.length - pairs.length,
    }, rows, xColumn, yColumn, aggregation, type, pairs.length);
  }
  if (type === "kpi") {
    const values = numericValuesFor(rows, yColumn);
    const sum = values.reduce((total, value) => total + value, 0);
    const observedCount = rows.filter((row) => row[yColumn] !== null && row[yColumn] !== undefined).length;
    const value = aggregation === "count" ? observedCount
      : aggregation === "mean" ? (values.length ? sum / values.length : null)
      : aggregation === "min" ? (values.length ? Math.min(...values) : null)
      : aggregation === "max" ? (values.length ? Math.max(...values) : null)
      : sum;
    return localMetric({ labels: [`${aggregation} of ${yColumn}`], values: [value], rows: [] }, rows, xColumn, yColumn, aggregation, type, aggregation === "count" ? observedCount : values.length);
  }
  if (type === "histogram") {
    const values = numericValuesFor(rows, yColumn);
    if (!values.length) return localMetric({ labels: [], values: [], rows: [] }, rows, xColumn, yColumn, aggregation, type, 0);
    const minimum = Math.min(...values);
    const maximum = Math.max(...values);
    if (minimum === maximum) {
      return localMetric({ labels: [String(minimum)], values: [values.length], rows: [] }, rows, xColumn, yColumn, aggregation, type, values.length);
    }
    const binCount = 10;
    const width = (maximum - minimum) / binCount;
    const counts = Array.from({ length: binCount }, () => 0);
    for (const value of values) {
      counts[Math.min(binCount - 1, Math.floor((value - minimum) / width))] += 1;
    }
    const formatter = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
    const labels = counts.map((_, index) => {
      const start = minimum + width * index;
      const end = index === binCount - 1 ? maximum : minimum + width * (index + 1);
      return `${formatter.format(start)}–${formatter.format(end)}`;
    });
    return localMetric({ labels, values: counts, rows: [] }, rows, xColumn, yColumn, aggregation, type, values.length);
  }
  const groups = new Map<string, ChartAccumulator>();
  for (const row of rows) {
    const key = String(row[xColumn] ?? "Unknown");
    const raw = row[yColumn];
    const parsed = finiteNumber(typeof raw === "boolean" ? Number(raw) : raw);
    const hasValue = parsed !== null;
    const observed = raw !== null && raw !== undefined;
    const current = groups.get(key);
    if (current) {
      if (observed) current.count += 1;
      if (hasValue) {
        current.numericCount += 1;
        current.sum += parsed;
        current.min = Math.min(current.min, parsed);
        current.max = Math.max(current.max, parsed);
      }
    } else {
      groups.set(key, {
        count: observed ? 1 : 0,
        numericCount: hasValue ? 1 : 0,
        sum: hasValue ? parsed : 0,
        min: hasValue ? parsed : Number.POSITIVE_INFINITY,
        max: hasValue ? parsed : Number.NEGATIVE_INFINITY,
      });
    }
  }
  const labels = [...groups.entries()]
    .filter(([, group]) => aggregation === "sum" || aggregation === "count" || group.numericCount > 0)
    .map(([label]) => label);
  const values = labels.map((label) => {
    const group = groups.get(label)!;
    if (aggregation === "count") return group.count;
    if (aggregation === "mean") return group.numericCount ? group.sum / group.numericCount : 0;
    if (aggregation === "min") return group.numericCount ? group.min : 0;
    if (aggregation === "max") return group.numericCount ? group.max : 0;
    return group.sum;
  });
  const validCount = [...groups.values()].reduce((total, group) => total + (aggregation === "count" ? group.count : group.numericCount), 0);
  return localMetric({ labels, values, rows: [] }, rows, xColumn, yColumn, aggregation, type, validCount);
}

const CHART_COLORS = ["#155eef", "#0e9384", "#7f56d9", "#f79009", "#d92d20", "#2e90fa", "#12b76a", "#ee46bc"];
const CHART_NUMBER_FORMAT = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

function chartValueHeading(data: ChartData, type: ChartType) {
  if (type === "scatter") return `Y: ${data.value_column || "selected Y value"}`;
  if (type === "histogram") return "Count of recorded values";
  const operation = data.aggregation || "sum";
  return operation === "count" && data.value_column === null
    ? "Count of source rows"
    : `${operation} of ${data.value_column || "selected measure"}`;
}

type ChartValueRow = { label: string | number; value: number | null; share?: string };

function ChartValuesTable({ id, data, type, rows, total }: {
  id: string; data: ChartData; type: ChartType; rows: ChartValueRow[]; total?: number;
}) {
  const category = type === "scatter" ? `X: ${data.group_column || "selected X value"}`
    : type === "histogram" ? `Interval of ${data.value_column || "selected measure"}`
    : data.group_column || "Category";
  const measure = chartValueHeading(data, type);
  const name = `${type[0].toUpperCase()}${type.slice(1)} chart values`;
  return <details className="chart-values" id={id}>
    <summary>View chart values</summary>
    <p>Values match the displayed chart, including its display limits and any combined categories. Numbers retain their available precision. Unavailable values are not zero.</p>
    {type === "scatter" ? <p>Each row is one displayed X/Y pair. Repeated X values remain separate observations.</p> : null}
    <table>
      <caption id={`${id}-caption`}>{name}: {measure}{type === "scatter" ? ` against ${category}` : ` by ${category}`}</caption>
      <thead><tr><th scope="col">{category}</th><th scope="col">{measure}</th>{type === "pie" ? <th scope="col">Share of total</th> : null}</tr></thead>
      <tbody>{rows.map((row, index) => <tr key={index}><th scope="row">{String(row.label)}</th><td>{row.value === null ? "Unavailable" : String(row.value)}</td>{type === "pie" ? <td>{row.share}</td> : null}</tr>)}</tbody>
      {total !== undefined ? <tfoot><tr><th scope="row">Total</th><td>{String(total)}</td><td>100%</td></tr></tfoot> : null}
    </table>
  </details>;
}

function ChartNotice({ children }: { children: ReactNode }) {
  return <p className="chart-notice" style={{ padding: "0 28px", color: "var(--muted)", fontSize: 12 }}>{children}</p>;
}

function ChartLimitNotice({ data, shown, available, unit }: { data: ChartData; shown: number; available: number; unit: string }) {
  if (!data.truncated && shown >= available) return null;
  return <ChartNotice>Showing the first {shown} of {data.total_count ?? available} {unit}. Remaining data is not plotted.</ChartNotice>;
}

export function ChartMetricSummary({ data, type }: { data: ChartData | null; type: ChartType }) {
  if (!data) return null;
  const measure = data.value_column || "selected measure";
  const group = data.group_column || "selected category";
  const aggregation = data.aggregation;
  const countRows = type === "pie" && data.value_column === null;
  const operation = aggregation === "mean" ? "Arithmetic mean" : aggregation === "min" ? "Minimum"
    : aggregation === "max" ? "Maximum" : aggregation === "count" ? "Count" : "Sum";
  const description = type === "table" ? "Rows from the current result, before any display limit."
    : type === "scatter" ? `One point is one source row: X = ${group}; Y = ${measure}. Repeated X values remain separate observations.`
    : type === "histogram" ? `Number of recorded ${measure} values in each interval. Unit: rows per interval.`
    : aggregation === "count" ? countRows ? `Count of source rows per ${group}. Unit: rows; duplicates count separately.` : `Count of non-null ${measure} values${type === "kpi" ? "" : ` per ${group}`}. Unit: recorded values; duplicates count separately, and empty text counts as a value.`
    : `${operation} of ${measure}${type === "kpi" ? " across the result" : ` calculated separately for each ${group}`}. Unit: the source unit of ${measure}.`;
  const omitted = data.omitted_count ?? 0;
  return <div aria-label="Metric definition">
    <ChartNotice>{description}</ChartNotice>
    {aggregation === "mean" ? <ChartNotice>Each usable source row has equal weight. Category averages are not added together or presented as a global average.</ChartNotice> : null}
    {type === "pie" ? <ChartNotice>Percentages show each category’s share of the {aggregation === "count" ? "count" : "sum"}. Other combines only additive category contributions.</ChartNotice> : null}
    {data.source_row_count != null ? <ChartNotice>Result scope: {data.source_row_count} source rows after filtering{data.valid_value_count != null && type !== "table" ? `; ${data.valid_value_count} ${type === "scatter" ? "usable X/Y pairs" : countRows ? "rows counted" : "recorded values used"}` : ""}.{omitted && type !== "scatter" ? ` ${omitted} ${aggregation === "count" ? "null" : "missing or non-finite"} measure values excluded.` : ""}</ChartNotice> : null}
    {data.valid_value_count === 0 && (data.source_row_count ?? 0) > 0 ? <ChartNotice>No usable observations remain. A zero sum or count does not mean a measured value of zero; averages and extrema are unavailable.</ChartNotice> : null}
  </div>;
}

export function ChartVisual({ data, type = "bar" }: { data: ChartData | null; type?: ChartType }) {
  const valuesId = useId();
  const numericValues = (data?.labels || []).map((_, index) => finiteNumber(data?.values[index]));
  if (type === "pie" && data?.aggregation && data.aggregation !== "sum" && data.aggregation !== "count") return <div className="empty-state"><strong>Choose an additive measure for a pie</strong><span>Use sum or count. Compare mean, min or max in a bar, line or KPI; they cannot form a meaningful part-to-whole total.</span></div>;
  if (data?.source_row_count === 0) return <div className="empty-state"><strong>No matching rows</strong><span>This chart’s current result contains no source rows.</span></div>;
  if (type === "table" && data?.rows.length) {
    const tableColumns = Object.keys(data.rows[0] || {});
    return <><ChartLimitNotice data={data} shown={Math.min(data.rows.length, 20)} available={data.rows.length} unit="rows" /><div className="chart-table"><table><thead><tr>{tableColumns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{data.rows.slice(0, 20).map((row, index) => <tr key={index}>{tableColumns.map((column) => <td key={column}>{String(row[column] ?? "—")}</td>)}</tr>)}</tbody></table></div></>;
  }
  if (type === "scatter" && data && !data.labels.length) return <div className="empty-state"><strong>No valid numeric pairs</strong><span>Scatter charts require finite numeric X and Y values in the same row.</span></div>;
  if (data && !data.labels.length) return <div className="empty-state"><strong>No usable values</strong><span>The result contains no values that this chart can display. Review the metric and missing-value counts.</span></div>;
  if (!data) return <div className="empty-state"><strong>No chart yet</strong><span>Choose dimensions and build a chart.</span></div>;
  if (type === "kpi") {
    const value = numericValues[0];
    const displayValue = value === null ? "—" : CHART_NUMBER_FORMAT.format(value);
    return <div className="kpi-visual"><span>{String(data.labels[0])}</span><strong>{displayValue}</strong></div>;
  }
  if (type === "pie") {
    if (data.truncated) return <div className="empty-state"><strong>Incomplete pie data</strong><span>A pie chart needs the complete total. Narrow the dataset or choose another chart type.</span></div>;
    if (numericValues.some((value) => value === null || value < 0)) return <div className="empty-state"><strong>Pie chart unavailable</strong><span>Pie values must be finite, non-negative numbers. Missing and negative values cannot be treated as zero.</span></div>;
    const categories = data.labels.map((label, index) => ({ label: String(label), value: numericValues[index]! }));
    const categoryCount = data.grouped_count ? (data.total_count ?? categories.length + data.grouped_count - 1) : categories.length;
    const total = categories.reduce((sum, category) => sum + category.value, 0);
    if (total <= 0 || !Number.isFinite(total)) return <div className="empty-state"><strong>No positive total</strong><span>A pie chart requires a finite total greater than zero.</span></div>;
    const slices = categories.length > 8
      ? [...categories.slice(0, 7), { label: `Other (${categoryCount - 7} categories)`, value: categories.slice(7).reduce((sum, category) => sum + category.value, 0) }]
      : categories;
    let cursor = 0;
    const stops = slices.map(({ value }, index) => {
      const start = cursor;
      cursor += (value / total) * 100;
      return `${CHART_COLORS[index % CHART_COLORS.length]} ${start}% ${cursor}%`;
    });
    return <>{categories.length > 8 ? <ChartNotice>All {categoryCount} categories are included. The final {categoryCount - 7} categories are combined as Other.</ChartNotice> : data.grouped_count ? <ChartNotice>All {categoryCount} categories are included; {data.grouped_count} categories are combined as Other.</ChartNotice> : null}<div className="pie-layout" role="img" aria-label="Generated pie chart" aria-describedby={`${valuesId}-caption`} aria-details={valuesId}><div className="pie-chart" style={{ background: `conic-gradient(${stops.join(",")})` }}><span>{CHART_NUMBER_FORMAT.format(total)}</span></div><div className="pie-legend">{slices.map(({ label, value }, index) => <div key={`${label}-${index}`}><i style={{ background: CHART_COLORS[index % CHART_COLORS.length] }} /><span title={`${label}: ${CHART_NUMBER_FORMAT.format(value)}`}>{label}</span><strong>{((value / total) * 100).toFixed(1)}%</strong></div>)}</div></div><ChartValuesTable id={valuesId} data={data} type={type} rows={slices.map((slice) => ({ ...slice, share: `${((slice.value / total) * 100).toFixed(1)}%` }))} total={total} /></>;
  }
  if (type === "scatter") {
    const pairs = data.labels.flatMap((label, index) => {
      const x = finiteNumber(label);
      const y = numericValues[index];
      return x === null || y === null ? [] : [{ x, y }];
    });
    const points = pairs.slice(0, 100);
    if (!points.length) return <div className="empty-state"><strong>No valid numeric pairs</strong><span>Scatter charts require finite numeric X and Y values in the same row.</span></div>;
    const xMin = Math.min(...points.map((point) => point.x));
    const xMax = Math.max(...points.map((point) => point.x));
    const yMin = Math.min(...points.map((point) => point.y));
    const yMax = Math.max(...points.map((point) => point.y));
    const omitted = (data.omitted_count ?? 0) + data.labels.length - pairs.length;
    return <><ChartLimitNotice data={data} shown={points.length} available={pairs.length} unit="numeric pairs" />{omitted ? <ChartNotice>{omitted} rows without a finite numeric X/Y pair were excluded.</ChartNotice> : null}<div className="line-chart" role="img" aria-label="Generated scatter chart" aria-describedby={`${valuesId}-caption`} aria-details={valuesId}><svg viewBox="0 0 600 260" preserveAspectRatio="none" aria-hidden="true"><path className="line-grid" d="M30 30H570 M30 80H570 M30 130H570 M30 180H570 M30 230H570" />{points.map((point, index) => <circle key={index} cx={xMin === xMax ? 300 : 30 + ((point.x - xMin) / (xMax - xMin)) * 540} cy={yMin === yMax ? 130 : 230 - ((point.y - yMin) / (yMax - yMin)) * 200} r="5"><title>{`X: ${point.x}; Y: ${point.y}`}</title></circle>)}</svg><div><span>X: {CHART_NUMBER_FORMAT.format(xMin)} to {CHART_NUMBER_FORMAT.format(xMax)}</span><span>Y: {CHART_NUMBER_FORMAT.format(yMin)} to {CHART_NUMBER_FORMAT.format(yMax)}</span></div></div><ChartValuesTable id={valuesId} data={data} type={type} rows={points.map((point) => ({ label: point.x, value: point.y }))} /></>;
  }
  if (type === "line") {
    const values = numericValues.slice(0, 24);
    const finiteValues = values.filter((value): value is number => value !== null);
    const minimum = Math.min(...finiteValues, 0);
    const maximum = Math.max(...finiteValues, 1);
    const range = Math.max(maximum - minimum, 1);
    const points = values.map((value, index) => value === null ? null : ({
      x: values.length === 1 ? 300 : 30 + (index / (values.length - 1)) * 540,
      y: 230 - ((value - minimum) / range) * 200,
    }));
    const segments: string[][] = [[]];
    for (const point of points) {
      if (point === null) segments.push([]);
      else segments[segments.length - 1].push(`${point.x},${point.y}`);
    }
    return <><ChartLimitNotice data={data} shown={values.length} available={data.labels.length} unit="categories" />{finiteValues.length < values.length ? <ChartNotice>Missing or invalid values are shown as gaps, not zero.</ChartNotice> : null}<div className="line-chart" role="img" aria-label="Generated line chart" aria-describedby={`${valuesId}-caption`} aria-details={valuesId}><svg viewBox="0 0 600 260" preserveAspectRatio="none" aria-hidden="true"><path className="line-grid" d="M30 30H570 M30 80H570 M30 130H570 M30 180H570 M30 230H570" />{segments.filter((segment) => segment.length > 1).map((segment, index) => <polyline key={index} points={segment.join(" ")} />)}{points.map((point, index) => point === null ? null : <circle key={index} cx={point.x} cy={point.y} r="5"><title>{`${data.labels[index]}: ${values[index]}`}</title></circle>)}</svg><div>{data.labels.slice(0, 24).map((label, index) => <small key={`${label}-${index}`} title={`${label}: ${values[index] ?? "unavailable"}`}>{String(label)}</small>)}</div></div><ChartValuesTable id={valuesId} data={data} type={type} rows={data.labels.slice(0, 24).map((label, index) => ({ label, value: values[index] }))} /></>;
  }
  const displayedValues = numericValues.slice(0, 12);
  const finiteValues = displayedValues.filter((value): value is number => value !== null);
  const minimum = Math.min(...finiteValues, 0);
  const maximum = Math.max(...finiteValues, 0);
  const range = maximum - minimum || 1;
  const baseline = (maximum / range) * 100;
  return <><ChartLimitNotice data={data} shown={displayedValues.length} available={data.labels.length} unit="categories" />{finiteValues.length < displayedValues.length ? <ChartNotice>Missing or invalid values are marked — and have no bar.</ChartNotice> : null}{minimum < 0 ? <ChartNotice>Negative values extend below the zero baseline.</ChartNotice> : null}<div className="bar-chart" role="img" aria-label="Generated data chart" aria-describedby={`${valuesId}-caption`} aria-details={valuesId}>{data.labels.slice(0, 12).map((label, index) => {
    const value = displayedValues[index];
    return <div className="bar-item" key={`${label}-${index}`}><div className="bar-value">{value === null ? "—" : CHART_NUMBER_FORMAT.format(value)}</div><div className="bar-track" style={{ position: "relative" }}><i aria-hidden="true" style={{ position: "absolute", top: `${baseline}%`, left: 0, right: 0, borderTop: "1px solid var(--border-strong)" }} />{value === null ? null : <span className={value < 0 ? "negative" : undefined} style={{ position: "absolute", top: `${value >= 0 ? ((maximum - value) / range) * 100 : baseline}%`, height: `${(Math.abs(value) / range) * 100}%`, left: "50%", transform: "translateX(-50%)" }} />}</div><small title={String(label)}>{String(label)}</small></div>;
  })}</div><ChartValuesTable id={valuesId} data={data} type={type} rows={data.labels.slice(0, 12).map((label, index) => ({ label, value: displayedValues[index] }))} /></>;
}

export function DashboardWorkspace({ live, projectId, datasetId, rows, columns, externalBusy, onNotice, onBusyChange }: Props) {
  const fieldId = useId();
  const [dashboards, setDashboards] = useState<Dashboard[]>([]);
  const [charts, setCharts] = useState<Chart[]>([]);
  const [data, setData] = useState<ChartData | null>(null);
  const [dataType, setDataType] = useState<ChartType>("bar");
  const [xColumn, setXColumn] = useState(columns.includes("category") ? "category" : columns[0] || "");
  const [yColumn, setYColumn] = useState(columns.includes("total") ? "total" : columns.find((column) => typeof rows[0]?.[column] === "number") || columns[0] || "");
  const [aggregation, setAggregation] = useState<Aggregation>("sum");
  const [chartType, setChartType] = useState<ChartType>("bar");
  const [visualTitle, setVisualTitle] = useState("");
  const [selectedDashboardId, setSelectedDashboardId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const interactiveRequests = useRef<InteractiveRequestFence | null>(null);
  if (!interactiveRequests.current) interactiveRequests.current = new InteractiveRequestFence();
  const requestFence = interactiveRequests.current;
  const numericColumns = useMemo(() => columns.filter((column) => rows.some((row) => typeof row[column] === "number")), [columns, rows]);
  const xColumns = chartType === "scatter" ? numericColumns : columns;
  const measureColumns = aggregation === "count" && chartType !== "scatter" && chartType !== "histogram" ? columns : numericColumns.length ? numericColumns : columns;
  const activeXColumn = xColumns.includes(xColumn) ? xColumn : xColumns[0] || "";
  const activeYColumn = measureColumns.includes(yColumn) ? yColumn : measureColumns[0] || "";
  const invalidPieAggregation = chartType === "pie" && aggregation !== "sum" && aggregation !== "count";

  useEffect(() => {
    const hadInteractiveRequest = requestFence.invalidate();
    setBusy(false);
    if (hadInteractiveRequest) onBusyChange(false);
    setDashboards([]);
    setCharts([]);
    setData(null);
    setSelectedDashboardId(null);
    setVisualTitle("");
    const controller = new AbortController();
    if (live && projectId) {
      void listDashboards(projectId, controller.signal)
        .then((items) => {
          if (controller.signal.aborted) return;
          setDashboards(items);
          setSelectedDashboardId((selected) => items.some((item) => item.id === selected) ? selected : items[0]?.id ?? null);
        })
        .catch((error) => { if (!isRequestCancelled(error)) setDashboards([]); });
    }
    return () => controller.abort();
  }, [live, projectId]);

  useEffect(() => () => {
    if (requestFence.invalidate()) onBusyChange(false);
  }, [onBusyChange]);

  async function selectDashboard(dashboard: Dashboard) {
    const request = requestFence.begin();
    setBusy(true);
    onBusyChange(true);
    try {
      const savedCharts = await listCharts(dashboard.id, request.controller.signal);
      if (!requestFence.isCurrent(request)) return;
      setSelectedDashboardId(dashboard.id);
      setCharts(savedCharts);
      if (savedCharts[0]) {
        const chartData = await fetchChartData(savedCharts[0].id, request.controller.signal);
        if (!requestFence.isCurrent(request)) return;
        setData(withChartDefinition(chartData, savedCharts[0]));
        setDataType(savedCharts[0].chart_type);
        setChartType(savedCharts[0].chart_type);
        setVisualTitle(savedCharts[0].title);
      } else {
        setData(null);
        setVisualTitle(dashboard.name);
      }
    }
    catch (error) {
      if (requestFence.isCurrent(request) && !isRequestCancelled(error)) onNotice(describeApiError(error));
    }
    finally {
      if (requestFence.finish(request)) {
        setBusy(false);
        onBusyChange(false);
      }
    }
  }

  async function buildChart() {
    if (invalidPieAggregation) {
      onNotice("Choose sum or count for a pie chart. Use a bar, line or KPI for mean, min or max.");
      return;
    }
    const request = requestFence.begin();
    setBusy(true);
    onBusyChange(true);
    try {
      const title = chartType === "table" ? "Dataset preview"
        : chartType === "kpi" ? `${aggregation} of ${activeYColumn}`
        : chartType === "histogram" ? `Distribution of ${activeYColumn}`
        : chartType === "scatter" ? `${activeYColumn} vs ${activeXColumn}`
        : `${aggregation} of ${activeYColumn} by ${activeXColumn}`;
      if (!live || !projectId || !datasetId) {
        if (!requestFence.isCurrent(request)) return;
        setData(buildLocalChartData(rows, activeXColumn, activeYColumn, aggregation, chartType));
        setDataType(chartType);
        setVisualTitle(title);
        onNotice("Interactive demo chart generated from the local preview (up to 100 rows)."); return;
      }
      let dashboard = dashboards.find((item) => item.id === selectedDashboardId) || dashboards[0];
      if (!dashboard) {
        dashboard = await createDashboard(projectId, "Executive performance dashboard", request.controller.signal);
        if (!requestFence.isCurrent(request)) return;
        setDashboards([dashboard]);
        setSelectedDashboardId(dashboard.id);
      }
      const chart = await createChart(dashboard.id, {
        dataset_id: datasetId,
        title,
        chart_type: chartType,
        x_column: chartType === "table" || chartType === "kpi" ? null : chartType === "histogram" ? activeYColumn : activeXColumn,
        y_column: chartType === "table" || chartType === "histogram" ? null : activeYColumn,
        aggregation,
      }, request.controller.signal);
      if (!requestFence.isCurrent(request)) return;
      const chartData = await fetchChartData(chart.id, request.controller.signal);
      if (!requestFence.isCurrent(request)) return;
      setCharts((items) => [chart, ...items]); setData(withChartDefinition(chartData, chart)); setDataType(chart.chart_type); setVisualTitle(chart.title);
      onNotice("Dashboard chart persisted and loaded from the API.");
    } catch (error) {
      if (requestFence.isCurrent(request) && !isRequestCancelled(error)) onNotice(describeApiError(error));
    }
    finally {
      if (requestFence.finish(request)) {
        setBusy(false);
        onBusyChange(false);
      }
    }
  }

  async function openChart(chart: Chart) {
    const request = requestFence.begin();
    setBusy(true);
    onBusyChange(true);
    try {
      const chartData = await fetchChartData(chart.id, request.controller.signal);
      if (!requestFence.isCurrent(request)) return;
      setData(withChartDefinition(chartData, chart)); setDataType(chart.chart_type); setChartType(chart.chart_type); setVisualTitle(chart.title);
    }
    catch (error) {
      if (requestFence.isCurrent(request) && !isRequestCancelled(error)) onNotice(describeApiError(error));
    }
    finally {
      if (requestFence.finish(request)) {
        setBusy(false);
        onBusyChange(false);
      }
    }
  }

  return <section className="dashboard-layout"><article className="panel chart-config"><div className="panel-header"><div><h2>Dashboard builder</h2><p>Turn transformed datasets into decision-ready visualizations</p></div><span className={live ? "status-pill success" : "status-pill"}>{live ? "Persisted" : "Demo"}</span></div>
    <div className="config-body"><label><span id={`${fieldId}-dimension`}>{chartType === "scatter" ? "X value" : "Dimension"}</span><select aria-labelledby={`${fieldId}-dimension`} value={activeXColumn} onChange={(event) => setXColumn(event.target.value)}>{xColumns.map((column) => <option key={column}>{column}</option>)}</select></label>
      <label><span id={`${fieldId}-measure`}>{chartType === "scatter" ? "Y value" : "Measure"}</span><select aria-labelledby={`${fieldId}-measure`} value={activeYColumn} onChange={(event) => setYColumn(event.target.value)}>{measureColumns.map((column) => <option key={column}>{column}</option>)}</select></label>
      <label><span id={`${fieldId}-aggregation`}>Aggregation</span><select aria-labelledby={`${fieldId}-aggregation`} value={aggregation} disabled={chartType === "scatter" || chartType === "table" || chartType === "histogram"} onChange={(event) => setAggregation(event.target.value as Aggregation)}>{["sum", "mean", "count", "min", "max"].map((item) => <option key={item} disabled={chartType === "pie" && item !== "sum" && item !== "count"}>{item}</option>)}</select></label>
      <label><span id={`${fieldId}-chart-type`}>Chart type</span><select aria-labelledby={`${fieldId}-chart-type`} value={chartType} onChange={(event) => setChartType(event.target.value as ChartType)}><option value="bar">Bar</option><option value="line">Line</option><option value="pie">Pie</option><option value="scatter">Scatter</option><option value="histogram">Histogram</option><option value="kpi">KPI</option><option value="table">Table</option></select></label>
      {chartType === "pie" ? <p>Pie charts compare shares of a sum or count. Choose a bar, line or KPI to compare averages, minima or maxima.</p> : null}
      <button className="primary" disabled={busy || externalBusy || !rows.length || invalidPieAggregation || (chartType === "scatter" && !numericColumns.length)} onClick={buildChart}>{busy ? "Building…" : "Build chart"}</button></div>
    {dashboards.length ? <div className="saved-items"><strong>Saved dashboards</strong>{dashboards.map((dashboard) => <button key={dashboard.id} disabled={busy || externalBusy} aria-pressed={dashboard.id === selectedDashboardId} onClick={() => selectDashboard(dashboard)}>{dashboard.name}</button>)}</div> : null}
  </article><article className="panel chart-canvas"><div className="panel-header"><div><h2>{visualTitle || `${aggregation} of ${activeYColumn} by ${activeXColumn}`}</h2><p>{data?.labels.length || data?.rows.length || 0} chart records loaded{live ? "" : " from the local preview"}</p></div></div><ChartMetricSummary data={data} type={dataType} /><ChartVisual data={data} type={dataType} />
    {charts.length ? <div className="chart-tabs">{charts.map((chart) => <button key={chart.id} disabled={busy || externalBusy} onClick={() => openChart(chart)}>{chart.title}</button>)}</div> : null}</article></section>;
}
