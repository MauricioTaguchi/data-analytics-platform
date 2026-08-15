import { useEffect, useMemo, useState } from "react";
import { checkApiHealth, describeApiError, downloadDataset, type AuthenticationInput } from "./api-client";
import { ConnectionPanel } from "./components/ConnectionPanel";
import { DashboardWorkspace } from "./components/DashboardWorkspace";
import { DatasetWorkspace } from "./components/DatasetWorkspace";
import { LineageTimeline } from "./components/LineageTimeline";
import { OperationsMonitor } from "./components/OperationsMonitor";
import { ReportsWorkspace } from "./components/ReportsWorkspace";
import { TransformationBuilder } from "./components/TransformationBuilder";
import { WorkspaceSidebar, type WorkspaceSection } from "./components/WorkspaceSidebar";
import { useAuthentication } from "./hooks/useAuthentication";
import { useDatasetUpload } from "./hooks/useDatasetUpload";
import { useProfilingJob } from "./hooks/useProfilingJob";
import { useTransformations } from "./hooks/useTransformations";

const TOUR_STEPS = [
  { title: "Start with trustworthy data", body: "Use the instant sample or connect the API to persist datasets, jobs, and lineage." },
  { title: "Measure before changing", body: "Quality, missing values, and duplicates remain visible before every transformation." },
  { title: "Deliver decisions, not files", body: "Build charts, export governed datasets, and generate auditable PDF reports." },
];

const SECTION_COPY: Record<WorkspaceSection, { title: string; subtitle: string }> = {
  overview: { title: "Analytics operations overview", subtitle: "A complete path from raw files to decision-ready deliverables." },
  datasets: { title: "Dataset workspace", subtitle: "Inspect schema, quality, completeness, and processing state." },
  transformations: { title: "Transformation pipeline", subtitle: "Preview, version, apply, and reverse governed data changes." },
  dashboards: { title: "Interactive dashboards", subtitle: "Create reusable charts backed by transformed datasets." },
  reports: { title: "Executive reports", subtitle: "Generate and download worker-rendered PDF summaries." },
  monitoring: { title: "Operations monitoring", subtitle: "Track background jobs, failures, progress, and cancellation." },
};

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob); const link = document.createElement("a");
  link.href = url; link.download = filename; link.click(); URL.revokeObjectURL(url);
}

export function App() {
  const auth = useAuthentication();
  const jobs = useProfilingJob();
  const workspace = useDatasetUpload();
  const transformations = useTransformations(workspace.dataset, workspace.rows, workspace.setRows, workspace.refresh, jobs.track);
  const [activeSection, setActiveSection] = useState<WorkspaceSection>("overview");
  const [notice, setNotice] = useState("The portfolio sample is ready. Connect the API to enable persistent workflows.");
  const [busy, setBusy] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [tourStep, setTourStep] = useState<number | null>(0);
  const [apiStatus, setApiStatus] = useState("not checked");
  const combinedBusy = busy || auth.busy;
  const progress = useMemo(() => jobs.jobs.find((job) => !new Set(["SUCCESS", "FAILURE", "REVOKED"]).has(job.status))?.progress ?? 100, [jobs.jobs]);

  useEffect(() => { if (workspace.dataset) void transformations.loadHistory(); }, [workspace.dataset?.id]);

  async function connect(input: AuthenticationInput) {
    try {
      const project = await auth.connect(input);
      setConnectionOpen(false); setNotice(`Connected to “${project.name}”. Upload a file to start the persistent workflow.`);
    } catch (error) { setNotice(describeApiError(error)); }
  }

  async function disconnect() {
    await auth.disconnect(); workspace.reset(); transformations.reset(); setNotice("The server session was revoked and the local demo was restored.");
  }

  async function upload(file?: File) {
    if (!file) return;
    setBusy(true); transformations.reset(); setNotice("Uploading and validating the dataset…");
    try {
      const result = await workspace.upload(file, auth.project?.id || null, jobs.track);
      setNotice(result.mode === "live" ? "Import and profiling completed through the worker queue." : "CSV loaded locally. Connect the API for durable processing.");
      setActiveSection("datasets");
    } catch (error) { setNotice(describeApiError(error)); } finally { setBusy(false); }
  }

  async function previewTransformation() {
    setBusy(true); try { await transformations.previewOperation(); setNotice("Transformation preview is ready for review."); }
    catch (error) { setNotice(describeApiError(error)); } finally { setBusy(false); }
  }

  async function applyTransformation() {
    setBusy(true); try { await transformations.applyOperation(); setNotice(auth.live ? "The version-controlled transformation completed successfully." : "The transformation was applied to the local demo."); }
    catch (error) { setNotice(describeApiError(error)); } finally { setBusy(false); }
  }

  async function undoTransformation() {
    setBusy(true); try { await transformations.undo(); setNotice("The latest completed transformation was reversed."); }
    catch (error) { setNotice(describeApiError(error)); } finally { setBusy(false); }
  }

  async function checkHealth() {
    setBusy(true); try { const status = await checkApiHealth(); setApiStatus(status); setNotice(`API readiness: ${status}.`); }
    catch { setApiStatus("unavailable"); setNotice("The API is unavailable. The local demo remains functional."); } finally { setBusy(false); }
  }

  async function exportDataset() {
    try {
      if (workspace.dataset) saveBlob(await downloadDataset(workspace.dataset.id), `processed-${workspace.datasetName}`);
      else {
        const csv = [workspace.columns.join(","), ...workspace.rows.map((row) => workspace.columns.map((column) => JSON.stringify(row[column] ?? "")).join(","))].join("\n");
        saveBlob(new Blob([csv], { type: "text/csv;charset=utf-8" }), `processed-${workspace.datasetName}`);
      }
      setNotice("The current dataset version is ready for download.");
    } catch (error) { setNotice(describeApiError(error)); }
  }

  const content = activeSection === "datasets" ? <DatasetWorkspace rows={workspace.rows} columns={workspace.columns} live={auth.live} progress={progress} />
    : activeSection === "transformations" ? <div className="work-grid"><TransformationBuilder live={auth.live} busy={combinedBusy} columns={workspace.columns} operation={transformations.operation} column={transformations.column} value={transformations.value} preview={transformations.preview} onOperation={transformations.setOperation} onColumn={transformations.setColumn} onValue={transformations.setValue} onPreview={previewTransformation} onApply={applyTransformation} /><LineageTimeline history={transformations.history} sourceName={workspace.datasetName} live={auth.live} canUndo={auth.live ? transformations.history.some((item) => item.status === "completed" && !item.undone_at) : transformations.localSnapshots.length > 0} busy={combinedBusy} onUndo={undoTransformation} /></div>
    : activeSection === "dashboards" ? <DashboardWorkspace live={auth.live} projectId={auth.project?.id || null} datasetId={workspace.dataset?.id || null} rows={workspace.rows} columns={workspace.columns} onNotice={setNotice} />
    : activeSection === "reports" ? <ReportsWorkspace live={auth.live} projectId={auth.project?.id || null} datasetId={workspace.dataset?.id || null} datasetName={workspace.datasetName} rows={workspace.dataset?.row_count || workspace.rows.length} columns={workspace.dataset?.column_count || workspace.columns.length} onNotice={setNotice} onTrack={jobs.track} />
    : activeSection === "monitoring" ? <OperationsMonitor jobs={jobs.jobs} live={auth.live} apiStatus={apiStatus} onCancel={(taskId) => void jobs.cancel(taskId).then(() => setNotice("The job was cancelled."), (error) => setNotice(describeApiError(error)))} />
    : <><section className="metric-grid hero-metrics"><div><span>Dataset rows</span><strong>{(workspace.dataset?.row_count || workspace.rows.length).toLocaleString("en-US")}</strong><small>Current governed version</small></div><div><span>Columns</span><strong>{workspace.dataset?.column_count || workspace.columns.length}</strong><small>Profiled attributes</small></div><div><span>Transformations</span><strong>{transformations.history.length}</strong><small>Auditable lineage events</small></div><div><span>Worker jobs</span><strong>{jobs.jobs.length}</strong><small>Tracked background tasks</small></div></section><DatasetWorkspace rows={workspace.rows} columns={workspace.columns} live={auth.live} progress={progress} /></>;

  return <div className="app-shell">
    <WorkspaceSidebar activeSection={activeSection} mobileOpen={mobileNav} live={auth.live} busy={combinedBusy} onNavigate={setActiveSection} onCloseMobile={() => setMobileNav(false)} onCheckApi={checkHealth} onConnect={() => setConnectionOpen(true)} onDisconnect={() => void disconnect()} />
    <main className="workspace"><header className="topbar"><button className="menu-button" onClick={() => setMobileNav((value) => !value)} aria-label="Open menu">☰</button><div className="breadcrumbs">DataFlow <span>›</span> {SECTION_COPY[activeSection].title}</div><div className="top-actions"><button className="secondary" onClick={() => { workspace.reset(); transformations.reset(); setNotice("The portfolio sample was restored."); }}>Use sample</button><label className="primary file-button">{combinedBusy ? "Processing…" : "Upload dataset"}<input type="file" accept=".csv,.xlsx,.xls,.json,.parquet" onChange={(event) => upload(event.target.files?.[0])} /></label></div></header>
      <section className="dataset-heading"><div><div className="title-line"><h1>{SECTION_COPY[activeSection].title}</h1><span className={auth.live ? "connected" : "connected demo"}>{auth.live ? "Live API" : "Demo mode"}</span></div><p>{SECTION_COPY[activeSection].subtitle}</p></div><button className="secondary tour-trigger" onClick={() => setTourStep(0)}>Guided tour</button></section>
      <div className="notice" role="status"><span>i</span>{notice}</div>{content}
      <footer className="workspace-footer"><div><span className="success-dot" />{auth.live ? "Authenticated, refreshable, revocable session" : "Local mode without persistence"}</div><div><button className="primary" onClick={exportDataset}>Export current version</button></div></footer>
    </main>
    {connectionOpen ? <ConnectionPanel busy={auth.busy} onClose={() => setConnectionOpen(false)} onSubmit={connect} /> : null}
    {tourStep !== null ? <div className="tour-backdrop" role="presentation"><section className="tour-dialog" role="dialog" aria-modal="true" aria-labelledby="tour-title"><span className="tour-progress">{tourStep + 1} of {TOUR_STEPS.length}</span><h2 id="tour-title">{TOUR_STEPS[tourStep].title}</h2><p>{TOUR_STEPS[tourStep].body}</p><div className="tour-dots" aria-hidden="true">{TOUR_STEPS.map((_, index) => <span key={index} className={index === tourStep ? "active" : ""} />)}</div><div className="button-row"><button className="secondary" onClick={() => setTourStep(null)}>Skip tour</button><button className="primary" autoFocus onClick={() => setTourStep(tourStep === TOUR_STEPS.length - 1 ? null : tourStep + 1)}>{tourStep === TOUR_STEPS.length - 1 ? "Explore DataFlow" : "Next"}</button></div></section></div> : null}
  </div>;
}
