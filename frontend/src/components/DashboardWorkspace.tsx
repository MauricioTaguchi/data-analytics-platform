import { useEffect, useMemo, useRef, useState } from "react";
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
    const raw = row[column];
    if (raw === null || raw === undefined || raw === "") return [];
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? [parsed] : [];
  });
}

export function buildLocalChartData(rows: DataRow[], xColumn: string, yColumn: string, aggregation: Aggregation, type: ChartType = "bar"): ChartData {
  if (type === "table") return { labels: [], values: [], rows: rows.slice(0, 100) };
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

export function ChartVisual({ data, type = "bar" }: { data: ChartData | null; type?: ChartType }) {
  const numericValues = (data?.values || []).map((value) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  });
  if (type === "table" && data?.rows.length) {
    const tableColumns = Object.keys(data.rows[0] || {});
    return <div className="chart-table"><table><thead><tr>{tableColumns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{data.rows.slice(0, 20).map((row, index) => <tr key={index}>{tableColumns.map((column) => <td key={column}>{String(row[column] ?? "—")}</td>)}</tr>)}</tbody></table></div>;
  }
  if (!data?.labels.length) return <div className="empty-state"><strong>No chart yet</strong><span>Choose dimensions and build a chart.</span></div>;
  if (type === "kpi") {
    const rawValue = data.values[0];
    const parsedValue = rawValue === null || rawValue === "" ? null : Number(rawValue);
    const displayValue = parsedValue !== null && Number.isFinite(parsedValue)
      ? new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(parsedValue)
      : "—";
    return <div className="kpi-visual"><span>{String(data.labels[0])}</span><strong>{displayValue}</strong></div>;
  }
  if (type === "pie") {
    const slices = numericValues.slice(0, 8).map((value) => Math.max(value, 0));
    const total = slices.reduce((sum, value) => sum + value, 0);
    let cursor = 0;
    const stops = slices.map((value, index) => {
      const start = cursor;
      cursor += total ? (value / total) * 100 : 0;
      return `${CHART_COLORS[index % CHART_COLORS.length]} ${start}% ${cursor}%`;
    });
    return <div className="pie-layout" role="img" aria-label="Generated pie chart"><div className="pie-chart" style={{ background: total ? `conic-gradient(${stops.join(",")})` : "#eaecf0" }}><span>{new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(total)}</span></div><div className="pie-legend">{data.labels.slice(0, 8).map((label, index) => <div key={`${label}-${index}`}><i style={{ background: CHART_COLORS[index % CHART_COLORS.length] }} /><span title={String(label)}>{String(label)}</span><strong>{total ? `${((slices[index] / total) * 100).toFixed(1)}%` : "0%"}</strong></div>)}</div></div>;
  }
  if (type === "line" || type === "scatter") {
    const values = numericValues.slice(0, 24);
    const minimum = Math.min(...values, 0);
    const maximum = Math.max(...values, 1);
    const range = Math.max(maximum - minimum, 1);
    const points = values.map((value, index) => ({
      x: values.length === 1 ? 300 : 30 + (index / (values.length - 1)) * 540,
      y: 230 - ((value - minimum) / range) * 200,
    }));
    return <div className="line-chart" role="img" aria-label={`Generated ${type} chart`}><svg viewBox="0 0 600 260" preserveAspectRatio="none" aria-hidden="true"><path className="line-grid" d="M30 30H570 M30 80H570 M30 130H570 M30 180H570 M30 230H570" />{type === "line" ? <polyline points={points.map((point) => `${point.x},${point.y}`).join(" ")} /> : null}{points.map((point, index) => <circle key={index} cx={point.x} cy={point.y} r="5"><title>{`${data.labels[index]}: ${values[index]}`}</title></circle>)}</svg><div>{data.labels.slice(0, 24).map((label, index) => <small key={`${label}-${index}`} title={String(label)}>{String(label)}</small>)}</div></div>;
  }
  const displayedValues = numericValues.slice(0, 12);
  const max = Math.max(...displayedValues.map((value) => Math.abs(value)), 1);
  return <div className="bar-chart" role="img" aria-label="Generated data chart">{data.labels.slice(0, 12).map((label, index) => <div className="bar-item" key={`${label}-${index}`}><div className="bar-value">{new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(displayedValues[index])}</div><div className="bar-track"><span className={displayedValues[index] < 0 ? "negative" : undefined} style={{ height: `${Math.max((Math.abs(displayedValues[index]) / max) * 100, 3)}%` }} /></div><small title={String(label)}>{String(label)}</small></div>)}</div>;
}

export function DashboardWorkspace({ live, projectId, datasetId, rows, columns, externalBusy, onNotice, onBusyChange }: Props) {
  const [dashboards, setDashboards] = useState<Dashboard[]>([]);
  const [charts, setCharts] = useState<Chart[]>([]);
  const [data, setData] = useState<ChartData | null>(null);
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
  const activeXColumn = columns.includes(xColumn) ? xColumn : columns[0] || "";
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
        : `${aggregation} of ${activeYColumn} by ${activeXColumn}`;
      if (!live || !projectId || !datasetId) {
        if (!requestFence.isCurrent(request)) return;
        setData(buildLocalChartData(rows, activeXColumn, activeYColumn, aggregation, chartType));
        setVisualTitle(title);
        onNotice("Interactive demo chart generated from the local dataset."); return;
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
      setCharts((items) => [chart, ...items]); setData(chartData); setVisualTitle(chart.title);
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
      setData(chartData); setChartType(chart.chart_type); setVisualTitle(chart.title);
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
    <div className="config-body"><label>Dimension<select value={activeXColumn} onChange={(event) => setXColumn(event.target.value)}>{columns.map((column) => <option key={column}>{column}</option>)}</select></label>
      <label>Measure<select value={activeYColumn} onChange={(event) => setYColumn(event.target.value)}>{(numericColumns.length ? numericColumns : columns).map((column) => <option key={column}>{column}</option>)}</select></label>
      <label>Aggregation<select value={aggregation} onChange={(event) => setAggregation(event.target.value as Aggregation)}>{["sum", "mean", "count", "min", "max"].map((item) => <option key={item}>{item}</option>)}</select></label>
      <label>Chart type<select value={chartType} onChange={(event) => setChartType(event.target.value as ChartType)}><option value="bar">Bar</option><option value="line">Line</option><option value="pie">Pie</option><option value="scatter">Scatter</option><option value="histogram">Histogram</option><option value="kpi">KPI</option><option value="table">Table</option></select></label>
      <button className="primary" disabled={busy || externalBusy || !rows.length} onClick={buildChart}>{busy ? "Building…" : "Build chart"}</button></div>
    {dashboards.length ? <div className="saved-items"><strong>Saved dashboards</strong>{dashboards.map((dashboard) => <button key={dashboard.id} disabled={busy || externalBusy} aria-pressed={dashboard.id === selectedDashboardId} onClick={() => selectDashboard(dashboard)}>{dashboard.name}</button>)}</div> : null}
  </article><article className="panel chart-canvas"><div className="panel-header"><div><h2>{visualTitle || `${aggregation} of ${activeYColumn} by ${activeXColumn}`}</h2><p>{data?.labels.length || data?.rows.length || 0} records visualized</p></div></div><ChartVisual data={data} type={chartType} />
    {charts.length ? <div className="chart-tabs">{charts.map((chart) => <button key={chart.id} disabled={busy || externalBusy} onClick={() => openChart(chart)}>{chart.title}</button>)}</div> : null}</article></section>;
}
