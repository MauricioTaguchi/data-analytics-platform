import React, { useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import axios from "axios";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  LineElement,
  PointElement,
  ArcElement,
  Tooltip,
  Legend,
} from "chart.js";
import { Bar, Line, Pie } from "react-chartjs-2";
import "./styles.css";

ChartJS.register(CategoryScale, LinearScale, BarElement, LineElement, PointElement, ArcElement, Tooltip, Legend);

const api = axios.create({ baseURL: "http://localhost:8000/api/v1" });

type Profile = {
  summary: {
    rows: number;
    columns: number;
    duplicate_rows: number;
    missing_cells: number;
    missing_percentage: number;
  };
  columns: Array<{
    name: string;
    dtype: string;
    missing_percentage: number;
    unique_count: number;
    outlier_count?: number;
  }>;
  suggestions: string[];
};

function App() {
  const [token, setToken] = useState(localStorage.getItem("token") || "");
  const [projectName, setProjectName] = useState("");
  const [projectId, setProjectId] = useState<number | null>(null);
  const [datasetId, setDatasetId] = useState<number | null>(null);
  const [dashboardId, setDashboardId] = useState<number | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [chartData, setChartData] = useState<any | null>(null);
  const [status, setStatus] = useState("Ready");

  const auth = useMemo(
    () => ({ headers: { Authorization: `Bearer ${token}` } }),
    [token]
  );

  async function register() {
    const response = await api.post("/auth/register", {
      name: "Mauricio",
      email: `mauricio${Date.now()}@example.com`,
      password: "senha12345",
    });
    localStorage.setItem("token", response.data.access_token);
    setToken(response.data.access_token);
    setStatus("Usuário autenticado");
  }

  async function createProject() {
    const response = await api.post(
      "/projects",
      { name: projectName, description: "Project created from the web interface" },
      auth
    );
    setProjectId(response.data.id);
    setStatus(`Projeto #${response.data.id} criado`);
  }

  async function upload() {
    if (!file || !projectId) return;
    const form = new FormData();
    form.append("file", file);
    const response = await api.post(`/datasets/project/${projectId}`, form, auth);
    setDatasetId(response.data.id);
    setStatus(`Dataset #${response.data.id} enviado`);
  }

  async function runProfile() {
    if (!datasetId) return;
    setStatus("Profiling em processamento...");
    const start = await api.post(`/datasets/${datasetId}/profile`, {}, auth);
    const taskId = start.data.task_id;
    if (taskId !== "cached") {
      for (let i = 0; i < 40; i++) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        const job = await api.get(`/datasets/jobs/${taskId}`, auth);
        if (job.data.status === "SUCCESS") break;
      }
    }
    const response = await api.get(`/datasets/${datasetId}/profile`, auth);
    setProfile(response.data.profile);
    setStatus("Profiling concluído");
  }

  async function createDashboardAndChart() {
    if (!projectId || !datasetId || !profile?.columns.length) return;
    const dashboard = await api.post(
      "/dashboards",
      { project_id: projectId, name: "Dashboard principal", description: "Visão automática", layout_json: {} },
      auth
    );
    setDashboardId(dashboard.data.id);

    const numeric = profile.columns.find((c) => c.dtype.includes("int") || c.dtype.includes("float"));
    const category = profile.columns.find((c) => !c.dtype.includes("int") && !c.dtype.includes("float"));
    const chart = await api.post(
      `/dashboards/${dashboard.data.id}/charts`,
      {
        dataset_id: datasetId,
        title: "Distribuição dos dados",
        chart_type: numeric && category ? "bar" : "pie",
        x_column: category?.name || profile.columns[0].name,
        y_column: numeric?.name || null,
        aggregation: numeric ? "sum" : null,
        filters_json: {}
      },
      auth
    );
    const data = await api.get(`/dashboards/charts/${chart.data.id}/data`, auth);
    setChartData(data.data);
    setStatus("Dashboard criado");
  }

  async function generateReport() {
    if (!projectId || !datasetId) return;
    const response = await api.post(`/reports/project/${projectId}/dataset/${datasetId}`, {}, auth);
    setStatus(`Relatório #${response.data.report_id} em geração`);
  }

  return (
    <main>
      <header className="hero">
        <div>
          <p className="eyebrow">ANALYTICS WORKSPACE</p>
          <h1>Data Analytics Platform</h1>
          <p>Produto completo para ingestão, qualidade, profiling, dashboards e relatórios.</p>
        </div>
        <div className="status">{status}</div>
      </header>

      <section className="actions">
        <article className="card"><span>01</span><h2>Conta</h2><button onClick={register}>Create user</button></article>
        <article className="card"><span>02</span><h2>Projeto</h2><input value={projectName} onChange={(e) => setProjectName(e.target.value)} placeholder="Sales Analysis"/><button disabled={!token || !projectName} onClick={createProject}>Create project</button></article>
        <article className="card"><span>03</span><h2>Dataset</h2><input type="file" accept=".csv,.xlsx,.xls,.json,.parquet" onChange={(e) => setFile(e.target.files?.[0] || null)}/><button disabled={!file || !projectId} onClick={upload}>Upload dataset</button></article>
        <article className="card"><span>04</span><h2>Profiling</h2><button disabled={!datasetId} onClick={runProfile}>Run profiling</button></article>
        <article className="card"><span>05</span><h2>Dashboard</h2><button disabled={!profile} onClick={createDashboardAndChart}>Criar gráfico</button></article>
        <article className="card"><span>06</span><h2>Relatório</h2><button disabled={!profile} onClick={generateReport}>Generate PDF</button></article>
      </section>

      {profile && (
        <>
          <section className="kpis">
            <div><strong>{profile.summary.rows}</strong><span>Rows</span></div>
            <div><strong>{profile.summary.columns}</strong><span>Columns</span></div>
            <div><strong>{profile.summary.duplicate_rows}</strong><span>Duplicates</span></div>
            <div><strong>{profile.summary.missing_percentage}%</strong><span>Missing data</span></div>
          </section>

          <section className="dashboard">
            <article className="panel">
              <h2>Qualidade por coluna</h2>
              <div className="bars">
                {profile.columns.slice(0, 10).map((column) => (
                  <div className="bar-row" key={column.name}>
                    <span>{column.name}</span>
                    <div className="bar-track"><div className="bar-fill" style={{ width: `${Math.min(column.missing_percentage, 100)}%` }} /></div>
                    <small>{column.missing_percentage}% nulos</small>
                  </div>
                ))}
              </div>
            </article>

            <article className="panel">
              <h2>Recommendations</h2>
              <ul>
                {profile.suggestions.length
                  ? profile.suggestions.map((item) => <li key={item}>{item}</li>)
                  : <li>Nenhum problema relevante identificado.</li>}
              </ul>
            </article>
          </section>
        </>
      )}

      {chartData && (
        <section className="panel chart-panel">
          <h2>Visualização gerada</h2>
          <Bar data={{ labels: chartData.labels, datasets: [{ label: "Valor", data: chartData.values }] }} />
        </section>
      )}
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
