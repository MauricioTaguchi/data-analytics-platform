import { useEffect, useMemo, useState } from "react";
import { createChart, createDashboard, fetchChartData, listCharts, listDashboards, type Aggregation, type Chart, type ChartData, type ChartType, type Dashboard } from "../api-client";
import type { DataRow } from "../data-utils";

type Props = { live: boolean; projectId: number | null; datasetId: number | null; rows: DataRow[]; columns: string[]; onNotice: (message: string) => void };

function localChart(rows: DataRow[], xColumn: string, yColumn: string, aggregation: Aggregation): ChartData {
  const groups = new Map<string, number[]>();
  for (const row of rows) {
    const key = String(row[xColumn] ?? "Unknown");
    const numeric = Number(row[yColumn]);
    groups.set(key, [...(groups.get(key) || []), Number.isFinite(numeric) ? numeric : 0]);
  }
  const labels = [...groups.keys()];
  const values = labels.map((label) => {
    const group = groups.get(label) || [];
    if (aggregation === "count") return group.length;
    if (aggregation === "mean") return group.reduce((total, value) => total + value, 0) / Math.max(group.length, 1);
    if (aggregation === "min") return Math.min(...group);
    if (aggregation === "max") return Math.max(...group);
    return group.reduce((total, value) => total + value, 0);
  });
  return { labels, values, rows: [] };
}

function ChartVisual({ data }: { data: ChartData | null }) {
  const numericValues = (data?.values || []).map(Number);
  const max = Math.max(...numericValues, 1);
  if (!data?.labels.length) return <div className="empty-state"><strong>No chart yet</strong><span>Choose dimensions and build a chart.</span></div>;
  return <div className="bar-chart" role="img" aria-label="Generated data chart">{data.labels.slice(0, 12).map((label, index) => <div className="bar-item" key={`${label}-${index}`}><div className="bar-value">{new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(numericValues[index])}</div><div className="bar-track"><span style={{ height: `${Math.max((numericValues[index] / max) * 100, 3)}%` }} /></div><small title={String(label)}>{String(label)}</small></div>)}</div>;
}

export function DashboardWorkspace({ live, projectId, datasetId, rows, columns, onNotice }: Props) {
  const [dashboards, setDashboards] = useState<Dashboard[]>([]);
  const [charts, setCharts] = useState<Chart[]>([]);
  const [data, setData] = useState<ChartData | null>(null);
  const [xColumn, setXColumn] = useState(columns.includes("category") ? "category" : columns[0] || "");
  const [yColumn, setYColumn] = useState(columns.includes("total") ? "total" : columns.find((column) => typeof rows[0]?.[column] === "number") || columns[0] || "");
  const [aggregation, setAggregation] = useState<Aggregation>("sum");
  const [chartType, setChartType] = useState<ChartType>("bar");
  const [busy, setBusy] = useState(false);
  const numericColumns = useMemo(() => columns.filter((column) => rows.some((row) => typeof row[column] === "number")), [columns, rows]);
  const activeXColumn = columns.includes(xColumn) ? xColumn : columns[0] || "";
  const activeYColumn = columns.includes(yColumn) ? yColumn : numericColumns[0] || columns[0] || "";

  useEffect(() => { if (projectId) void listDashboards(projectId).then(setDashboards).catch(() => setDashboards([])); else { setDashboards([]); setCharts([]); } }, [projectId]);

  async function selectDashboard(dashboard: Dashboard) {
    setCharts(await listCharts(dashboard.id));
  }

  async function buildChart() {
    setBusy(true);
    try {
      if (!live || !projectId || !datasetId) {
        setData(localChart(rows, activeXColumn, activeYColumn, aggregation));
        onNotice("Interactive demo chart generated from the local dataset."); return;
      }
      let dashboard = dashboards[0];
      if (!dashboard) {
        dashboard = await createDashboard(projectId, "Executive performance dashboard");
        setDashboards([dashboard]);
      }
      const chart = await createChart(dashboard.id, { dataset_id: datasetId, title: `${aggregation} of ${activeYColumn} by ${activeXColumn}`, chart_type: chartType, x_column: activeXColumn, y_column: activeYColumn, aggregation });
      const chartData = await fetchChartData(chart.id);
      setCharts((items) => [chart, ...items]); setData(chartData);
      onNotice("Dashboard chart persisted and loaded from the API.");
    } catch (error) { onNotice(error instanceof Error ? error.message : "The chart could not be generated."); }
    finally { setBusy(false); }
  }

  async function openChart(chart: Chart) { setBusy(true); try { setData(await fetchChartData(chart.id)); } finally { setBusy(false); } }

  return <section className="dashboard-layout"><article className="panel chart-config"><div className="panel-header"><div><h2>Dashboard builder</h2><p>Turn transformed datasets into decision-ready visualizations</p></div><span className={live ? "status-pill success" : "status-pill"}>{live ? "Persisted" : "Demo"}</span></div>
    <div className="config-body"><label>Dimension<select value={activeXColumn} onChange={(event) => setXColumn(event.target.value)}>{columns.map((column) => <option key={column}>{column}</option>)}</select></label>
      <label>Measure<select value={activeYColumn} onChange={(event) => setYColumn(event.target.value)}>{(numericColumns.length ? numericColumns : columns).map((column) => <option key={column}>{column}</option>)}</select></label>
      <label>Aggregation<select value={aggregation} onChange={(event) => setAggregation(event.target.value as Aggregation)}>{["sum", "mean", "count", "min", "max"].map((item) => <option key={item}>{item}</option>)}</select></label>
      <label>Chart type<select value={chartType} onChange={(event) => setChartType(event.target.value as ChartType)}><option value="bar">Bar</option><option value="line">Line</option><option value="pie">Pie</option></select></label>
      <button className="primary" disabled={busy || !rows.length} onClick={buildChart}>{busy ? "Building…" : "Build chart"}</button></div>
    {dashboards.length ? <div className="saved-items"><strong>Saved dashboards</strong>{dashboards.map((dashboard) => <button key={dashboard.id} onClick={() => selectDashboard(dashboard)}>{dashboard.name}</button>)}</div> : null}
  </article><article className="panel chart-canvas"><div className="panel-header"><div><h2>{aggregation} of {activeYColumn} by {activeXColumn}</h2><p>{data?.labels.length || 0} categories visualized</p></div></div><ChartVisual data={data} />
    {charts.length ? <div className="chart-tabs">{charts.map((chart) => <button key={chart.id} onClick={() => openChart(chart)}>{chart.title}</button>)}</div> : null}</article></section>;
}
