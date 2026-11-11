import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { createChart, createDashboard, describeApiError, fetchChartData, isRequestCancelled, listCharts, listDashboards, type Aggregation, type Chart, type ChartData, type ChartType, type Dashboard } from "../api-client";
import type { DataRow } from "../data-utils";

type Props = { live: boolean; projectId: number | null; datasetId: number | null; rows: DataRow[]; columns: string[]; externalBusy: boolean; onNotice: (message: string) => void; onBusyChange: (busy: boolean) => void };

type ChartAccumulator = { count: number; sum: number; min: number; max: number };
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
    const value = finiteNumber(row[column]);
    return value === null ? [] : [value];
  });
}

function finiteNumber(value: unknown): number | null {
  if (value === null || value === undefined || typeof value === "boolean"
    || (typeof value === "string" && value.trim() === "")) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function buildLocalChartData(rows: DataRow[], xColumn: string, yColumn: string, aggregation: Aggregation, type: ChartType = "bar"): ChartData {
  if (type === "table") return { labels: [], values: [], rows: rows.slice(0, 100), total_count: rows.length, returned_count: Math.min(rows.length, 100), truncated: rows.length > 100 };
  if (type === "scatter") {
    const pairs = rows.flatMap((row) => {
      const x = finiteNumber(row[xColumn]);
      const y = finiteNumber(row[yColumn]);
      return x === null || y === null ? [] : [{ x, y }];
    });
    return {
      labels: pairs.slice(0, 100).map((pair) => pair.x),
      values: pairs.slice(0, 100).map((pair) => pair.y),
      rows: [],
      total_count: pairs.length,
      returned_count: Math.min(pairs.length, 100),
      truncated: pairs.length > 100,
      omitted_count: rows.length - pairs.length,
    };
  }
  if (type === "kpi") {
    const values = numericValuesFor(rows, yColumn);
    const sum = values.reduce((total, value) => total + value, 0);
    const value = aggregation === "count" ? values.length
      : aggregation === "mean" ? (values.length ? sum / values.length : null)
      : aggregation === "min" ? (values.length ? Math.min(...values) : null)
      : aggregation === "max" ? (values.length ? Math.max(...values) : null)
      : sum;
    return { labels: [`${aggregation} of ${yColumn}`], values: [value], rows: [] };
  }
  if (type === "histogram") {
    const values = numericValuesFor(rows, yColumn);
    if (!values.length) return { labels: [], values: [], rows: [] };
    const minimum = Math.min(...values);
    const maximum = Math.max(...values);
    if (minimum === maximum) {
      return { labels: [String(minimum)], values: [values.length], rows: [] };
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
    return { labels, values: counts, rows: [] };
  }
  const groups = new Map<string, ChartAccumulator>();
  for (const row of rows) {
    const key = String(row[xColumn] ?? "Unknown");
    const raw = row[yColumn];
    const parsed = raw === null || raw === undefined || raw === "" ? Number.NaN : Number(raw);
    const hasValue = Number.isFinite(parsed);
    const current = groups.get(key);
    if (current) {
      if (hasValue) {
        current.count += 1;
        current.sum += parsed;
        current.min = Math.min(current.min, parsed);
        current.max = Math.max(current.max, parsed);
      }
    } else {
      groups.set(key, {
        count: hasValue ? 1 : 0,
        sum: hasValue ? parsed : 0,
        min: hasValue ? parsed : Number.POSITIVE_INFINITY,
        max: hasValue ? parsed : Number.NEGATIVE_INFINITY,
      });
    }
  }
  const labels = [...groups.entries()]
    .filter(([, group]) => aggregation === "sum" || aggregation === "count" || group.count > 0)
    .map(([label]) => label);
  const values = labels.map((label) => {
    const group = groups.get(label)!;
    if (aggregation === "count") return group.count;
    if (aggregation === "mean") return group.count ? group.sum / group.count : 0;
    if (aggregation === "min") return group.count ? group.min : 0;
    if (aggregation === "max") return group.count ? group.max : 0;
    return group.sum;
  });
  return { labels, values, rows: [] };
}

const CHART_COLORS = ["#155eef", "#0e9384", "#7f56d9", "#f79009", "#d92d20", "#2e90fa", "#12b76a", "#ee46bc"];
const CHART_NUMBER_FORMAT = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

function ChartNotice({ children }: { children: ReactNode }) {
  return <p className="chart-notice" style={{ padding: "0 28px", color: "var(--muted)", fontSize: 12 }}>{children}</p>;
}

function ChartLimitNotice({ data, shown, available, unit }: { data: ChartData; shown: number; available: number; unit: string }) {
  if (!data.truncated && shown >= available) return null;
  return <ChartNotice>Showing the first {shown} of {data.total_count ?? available} {unit}. Remaining data is not plotted.</ChartNotice>;
}

export function ChartVisual({ data, type = "bar" }: { data: ChartData | null; type?: ChartType }) {
  const numericValues = (data?.labels || []).map((_, index) => finiteNumber(data?.values[index]));
  if (type === "table" && data?.rows.length) {
    const tableColumns = Object.keys(data.rows[0] || {});
    return <><ChartLimitNotice data={data} shown={Math.min(data.rows.length, 20)} available={data.rows.length} unit="rows" /><div className="chart-table"><table><thead><tr>{tableColumns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{data.rows.slice(0, 20).map((row, index) => <tr key={index}>{tableColumns.map((column) => <td key={column}>{String(row[column] ?? "—")}</td>)}</tr>)}</tbody></table></div></>;
  }
  if (type === "scatter" && data && !data.labels.length) return <div className="empty-state"><strong>No valid numeric pairs</strong><span>Scatter charts require finite numeric X and Y values in the same row.</span></div>;
  if (!data?.labels.length) return <div className="empty-state"><strong>No chart yet</strong><span>Choose dimensions and build a chart.</span></div>;
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
    return <>{categories.length > 8 ? <ChartNotice>All {categoryCount} categories are included. The final {categoryCount - 7} categories are combined as Other.</ChartNotice> : data.grouped_count ? <ChartNotice>All {categoryCount} categories are included; {data.grouped_count} categories are combined as Other.</ChartNotice> : null}<div className="pie-layout" role="img" aria-label="Generated pie chart"><div className="pie-chart" style={{ background: `conic-gradient(${stops.join(",")})` }}><span>{CHART_NUMBER_FORMAT.format(total)}</span></div><div className="pie-legend">{slices.map(({ label, value }, index) => <div key={`${label}-${index}`}><i style={{ background: CHART_COLORS[index % CHART_COLORS.length] }} /><span title={`${label}: ${CHART_NUMBER_FORMAT.format(value)}`}>{label}</span><strong>{((value / total) * 100).toFixed(1)}%</strong></div>)}</div></div></>;
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
    return <><ChartLimitNotice data={data} shown={points.length} available={pairs.length} unit="numeric pairs" />{omitted ? <ChartNotice>{omitted} rows without a finite numeric X/Y pair were excluded.</ChartNotice> : null}<div className="line-chart" role="img" aria-label="Generated scatter chart"><svg viewBox="0 0 600 260" preserveAspectRatio="none" aria-hidden="true"><path className="line-grid" d="M30 30H570 M30 80H570 M30 130H570 M30 180H570 M30 230H570" />{points.map((point, index) => <circle key={index} cx={xMin === xMax ? 300 : 30 + ((point.x - xMin) / (xMax - xMin)) * 540} cy={yMin === yMax ? 130 : 230 - ((point.y - yMin) / (yMax - yMin)) * 200} r="5"><title>{`X: ${point.x}; Y: ${point.y}`}</title></circle>)}</svg><div><span>X: {CHART_NUMBER_FORMAT.format(xMin)} to {CHART_NUMBER_FORMAT.format(xMax)}</span><span>Y: {CHART_NUMBER_FORMAT.format(yMin)} to {CHART_NUMBER_FORMAT.format(yMax)}</span></div></div></>;
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
    return <><ChartLimitNotice data={data} shown={values.length} available={data.labels.length} unit="categories" />{finiteValues.length < values.length ? <ChartNotice>Missing or invalid values are shown as gaps, not zero.</ChartNotice> : null}<div className="line-chart" role="img" aria-label="Generated line chart"><svg viewBox="0 0 600 260" preserveAspectRatio="none" aria-hidden="true"><path className="line-grid" d="M30 30H570 M30 80H570 M30 130H570 M30 180H570 M30 230H570" />{segments.filter((segment) => segment.length > 1).map((segment, index) => <polyline key={index} points={segment.join(" ")} />)}{points.map((point, index) => point === null ? null : <circle key={index} cx={point.x} cy={point.y} r="5"><title>{`${data.labels[index]}: ${values[index]}`}</title></circle>)}</svg><div>{data.labels.slice(0, 24).map((label, index) => <small key={`${label}-${index}`} title={`${label}: ${values[index] ?? "unavailable"}`}>{String(label)}</small>)}</div></div></>;
  }
  const displayedValues = numericValues.slice(0, 12);
  const finiteValues = displayedValues.filter((value): value is number => value !== null);
  const minimum = Math.min(...finiteValues, 0);
  const maximum = Math.max(...finiteValues, 0);
  const range = maximum - minimum || 1;
  const baseline = (maximum / range) * 100;
  return <><ChartLimitNotice data={data} shown={displayedValues.length} available={data.labels.length} unit="categories" />{finiteValues.length < displayedValues.length ? <ChartNotice>Missing or invalid values are marked — and have no bar.</ChartNotice> : null}{minimum < 0 ? <ChartNotice>Negative values extend below the zero baseline.</ChartNotice> : null}<div className="bar-chart" role="img" aria-label="Generated data chart">{data.labels.slice(0, 12).map((label, index) => {
    const value = displayedValues[index];
    return <div className="bar-item" key={`${label}-${index}`}><div className="bar-value">{value === null ? "—" : CHART_NUMBER_FORMAT.format(value)}</div><div className="bar-track" style={{ position: "relative" }}><i aria-hidden="true" style={{ position: "absolute", top: `${baseline}%`, left: 0, right: 0, borderTop: "1px solid var(--border-strong)" }} />{value === null ? null : <span className={value < 0 ? "negative" : undefined} style={{ position: "absolute", top: `${value >= 0 ? ((maximum - value) / range) * 100 : baseline}%`, height: `${(Math.abs(value) / range) * 100}%`, left: "50%", transform: "translateX(-50%)" }} />}</div><small title={String(label)}>{String(label)}</small></div>;
  })}</div></>;
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
  const activeXColumn = xColumns.includes(xColumn) ? xColumn : xColumns[0] || "";
  const activeYColumn = columns.includes(yColumn) ? yColumn : numericColumns[0] || columns[0] || "";

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
        setData(chartData);
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
      setCharts((items) => [chart, ...items]); setData(chartData); setDataType(chart.chart_type); setVisualTitle(chart.title);
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
      setData(chartData); setDataType(chart.chart_type); setChartType(chart.chart_type); setVisualTitle(chart.title);
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
      <label><span id={`${fieldId}-measure`}>{chartType === "scatter" ? "Y value" : "Measure"}</span><select aria-labelledby={`${fieldId}-measure`} value={activeYColumn} onChange={(event) => setYColumn(event.target.value)}>{(numericColumns.length ? numericColumns : columns).map((column) => <option key={column}>{column}</option>)}</select></label>
      <label><span id={`${fieldId}-aggregation`}>Aggregation</span><select aria-labelledby={`${fieldId}-aggregation`} value={aggregation} disabled={chartType === "scatter" || chartType === "table" || chartType === "histogram"} onChange={(event) => setAggregation(event.target.value as Aggregation)}>{["sum", "mean", "count", "min", "max"].map((item) => <option key={item}>{item}</option>)}</select></label>
      <label><span id={`${fieldId}-chart-type`}>Chart type</span><select aria-labelledby={`${fieldId}-chart-type`} value={chartType} onChange={(event) => setChartType(event.target.value as ChartType)}><option value="bar">Bar</option><option value="line">Line</option><option value="pie">Pie</option><option value="scatter">Scatter</option><option value="histogram">Histogram</option><option value="kpi">KPI</option><option value="table">Table</option></select></label>
      <button className="primary" disabled={busy || externalBusy || !rows.length || (chartType === "scatter" && !numericColumns.length)} onClick={buildChart}>{busy ? "Building…" : "Build chart"}</button></div>
    {dashboards.length ? <div className="saved-items"><strong>Saved dashboards</strong>{dashboards.map((dashboard) => <button key={dashboard.id} disabled={busy || externalBusy} aria-pressed={dashboard.id === selectedDashboardId} onClick={() => selectDashboard(dashboard)}>{dashboard.name}</button>)}</div> : null}
  </article><article className="panel chart-canvas"><div className="panel-header"><div><h2>{visualTitle || `${aggregation} of ${activeYColumn} by ${activeXColumn}`}</h2><p>{data?.labels.length || data?.rows.length || 0} chart records loaded{live ? "" : " from the local preview"}</p></div></div><ChartVisual data={data} type={dataType} />
    {charts.length ? <div className="chart-tabs">{charts.map((chart) => <button key={chart.id} disabled={busy || externalBusy} onClick={() => openChart(chart)}>{chart.title}</button>)}</div> : null}</article></section>;
}
