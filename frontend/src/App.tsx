import { useEffect, useRef, useState } from "react";
import { checkApiHealth, describeApiError, downloadDataset, type AuthenticationInput } from "./api-client";
import { ConnectionPanel } from "./components/ConnectionPanel";
import { DashboardWorkspace } from "./components/DashboardWorkspace";
import { DatasetWorkspace } from "./components/DatasetWorkspace";
import { LineageTimeline } from "./components/LineageTimeline";
import { OperationsMonitor } from "./components/OperationsMonitor";
import { ReportsWorkspace } from "./components/ReportsWorkspace";
import { QualityContracts } from "./components/QualityContracts";
import { SnapshotHistory } from "./components/SnapshotHistory";
import { TransformationBuilder } from "./components/TransformationBuilder";
import { WorkspaceSidebar, type WorkspaceSection } from "./components/WorkspaceSidebar";
import { WorkspaceIcon, type WorkspaceIconName } from "./components/WorkspaceIcon";
import { useAuthentication } from "./hooks/useAuthentication";
import { useDatasetUpload } from "./hooks/useDatasetUpload";
import { useDialogFocus } from "./hooks/useDialogFocus";
import { isTerminalJobStatus, useProfilingJob } from "./hooks/useProfilingJob";
import { useTransformations } from "./hooks/useTransformations";
import { useTheme } from "./hooks/useTheme";

const TOUR_STEPS = [
  { title: "Start with trustworthy data", body: "Use the instant sample or connect the API to persist datasets, jobs, and lineage." },
  { title: "Measure before changing", body: "Quality, missing values, and duplicates remain visible before every transformation." },
  { title: "Deliver decisions, not files", body: "Build charts, export governed datasets, and generate auditable PDF reports." },
];

const SECTION_COPY: Record<WorkspaceSection, { title: string; subtitle: string }> = {
  overview: { title: "Analytics overview", subtitle: "Explore, transform, and trace your data." },
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
  const { theme, toggleTheme } = useTheme();
  const auth = useAuthentication();
  const jobs = useProfilingJob();
  const workspace = useDatasetUpload();
  const transformations = useTransformations(workspace.dataset, workspace.rows, workspace.setRows, workspace.refresh, jobs.track, workspace.columns, auth.live);
  const [activeSection, setActiveSection] = useState<WorkspaceSection>("overview");
  const [notice, setNotice] = useState("The portfolio sample is ready. Connect the API to enable persistent workflows.");
  const [busy, setBusy] = useState(false);
  const [lifecycleBusy, setLifecycleBusy] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [tourStep, setTourStep] = useState<number | null>(0);
  const tourDialog = useDialogFocus(tourStep !== null, () => setTourStep(null));
  const [apiStatus, setApiStatus] = useState("not checked");
  const lifecycleOperation = useRef(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const combinedBusy = busy || lifecycleBusy || auth.busy;
  const progress = jobs.jobs.find((job) => !isTerminalJobStatus(job.status))?.progress
    ?? (workspace.uploadPhase === "uploading" ? workspace.uploadProgress : 100);
  const uploadLabel = workspace.uploadPhase === "uploading"
    ? `Uploading ${workspace.uploadProgress}%`
    : combinedBusy ? "Processing…" : "Upload dataset";
  const rowCount = workspace.dataset?.row_count ?? workspace.rows.length;
  const columnCount = workspace.dataset?.column_count ?? workspace.columns.length;
  const sectionLabel = activeSection.charAt(0).toUpperCase() + activeSection.slice(1);
  const metrics: { label: string; value: number; caption: string; icon: WorkspaceIconName }[] = [
    { label: workspace.dataset ? "Dataset rows" : "Preview rows", value: rowCount, caption: workspace.dataset ? "Current dataset version" : "Rows in the local preview", icon: "rows" },
    { label: "Columns", value: columnCount, caption: "Attributes in this dataset", icon: "columns" },
    { label: "Transformations", value: transformations.history.length, caption: "Recorded history events", icon: "transformations" },
    { label: "Worker jobs", value: jobs.jobs.length, caption: "Tracked background tasks", icon: "monitoring" },
  ];

  function closeMobileNav() {
    setMobileNav(false);
    menuButton.current?.focus();
  }

  useEffect(() => {
    if (!mobileNav) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setMobileNav(false);
        menuButton.current?.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [mobileNav]);

  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 761px)");
    const closeOnDesktop = () => { if (desktop.matches) setMobileNav(false); };
    desktop.addEventListener("change", closeOnDesktop);
    return () => desktop.removeEventListener("change", closeOnDesktop);
  }, []);

  useEffect(() => {
    if (auth.sessionExpiredAt === null) return;
    jobs.reset();
    workspace.reset();
    transformations.reset();
    setBusy(false);
    setNotice("Your session expired. Server-backed data was cleared from this browser; reconnect to continue.");
  }, [auth.sessionExpiredAt, jobs.reset, transformations.reset, workspace.reset]);

  async function connect(input: AuthenticationInput) {
    try {
      const project = await auth.connect(input);
      setConnectionOpen(false); setNotice(`Connected to “${project.name}”. Upload a file to start the persistent workflow.`);
    } catch (error) { setNotice(describeApiError(error)); }
  }

  async function runLifecycleOperation(operation: () => Promise<void>) {
    if (lifecycleOperation.current) return;
    lifecycleOperation.current = true;
    setLifecycleBusy(true);
    try {
      await operation();
    } finally {
      lifecycleOperation.current = false;
      setLifecycleBusy(false);
    }
  }

  async function cancelActiveWorkflows() {
    const uploadTaskId = await workspace.cancelUpload((taskId, options) => jobs.cancel(taskId, options));
    await Promise.allSettled(jobs.jobs
      .filter((job) => job.task_id !== uploadTaskId && !isTerminalJobStatus(job.status))
      .map((job) => jobs.cancel(job.task_id)));
  }

  async function disconnect() {
    await runLifecycleOperation(async () => {
      await cancelActiveWorkflows();
      jobs.reset();
      workspace.reset();
      transformations.reset();
      await auth.disconnect();
      setNotice("The server session was revoked, active work was cancelled, and the local demo was restored.");
    });
  }

  async function restoreSample() {
    await runLifecycleOperation(async () => {
      await cancelActiveWorkflows();
      jobs.reset();
      workspace.reset();
      transformations.reset();
      setNotice("The portfolio sample was restored.");
    });
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
      if (workspace.dataset) {
        const download = await downloadDataset(workspace.dataset.id);
        saveBlob(download.blob, download.filename);
      } else {
        const csv = [workspace.columns.join(","), ...workspace.rows.map((row) => workspace.columns.map((column) => JSON.stringify(row[column] ?? "")).join(","))].join("\n");
        saveBlob(new Blob([csv], { type: "text/csv;charset=utf-8" }), `processed-${workspace.datasetName}`);
      }
      setNotice("The current dataset version is ready for download.");
    } catch (error) { setNotice(describeApiError(error)); }
  }

  const content = activeSection === "datasets" ? <><DatasetWorkspace rows={workspace.rows} columns={workspace.columns} live={auth.live} progress={progress} /><QualityContracts key={workspace.dataset?.id || "demo"} datasetId={workspace.dataset?.id || null} version={workspace.dataset?.version || 0} columns={workspace.columns} busy={combinedBusy} onTrack={jobs.track} /></>
    : activeSection === "transformations" ? <div className="work-grid"><TransformationBuilder live={auth.live} busy={combinedBusy || transformations.undoNeedsRecovery || (auth.live && !workspace.dataset)} columns={workspace.columns} operation={transformations.operation} draft={transformations.draft} engine={transformations.engine} catalog={transformations.catalog} catalogError={transformations.catalogError} preview={transformations.preview} onOperation={transformations.setOperation} onDraft={transformations.setDraft} onEngine={transformations.setEngine} onRetryCatalog={transformations.retryCatalog} onPreview={previewTransformation} onApply={applyTransformation} /><div className="side-stack"><LineageTimeline history={transformations.history} sourceName={workspace.datasetName} live={auth.live} canUndo={auth.live ? transformations.history.some((item) => item.status === "completed" && !item.undone_at) : transformations.localSnapshots.length > 0} busy={combinedBusy} undoNeedsRecovery={transformations.undoNeedsRecovery} onUndo={undoTransformation} />{workspace.dataset ? <SnapshotHistory key={workspace.dataset.id} datasetId={workspace.dataset.id} version={workspace.dataset.version} busy={combinedBusy} /> : null}</div></div>
    : activeSection === "dashboards" ? <DashboardWorkspace live={auth.live} projectId={auth.project?.id || null} datasetId={workspace.dataset?.id || null} rows={workspace.rows} columns={workspace.columns} externalBusy={combinedBusy} onNotice={setNotice} onBusyChange={setBusy} />
    : activeSection === "reports" ? <ReportsWorkspace live={auth.live} projectId={auth.project?.id || null} datasetId={workspace.dataset?.id || null} datasetName={workspace.datasetName} rows={workspace.dataset?.row_count || workspace.rows.length} columns={workspace.dataset?.column_count || workspace.columns.length} externalBusy={combinedBusy} onNotice={setNotice} onTrack={jobs.track} onBusyChange={setBusy} />
    : activeSection === "monitoring" ? <OperationsMonitor jobs={jobs.jobs} live={auth.live} apiStatus={apiStatus} onResume={(taskId) => void jobs.resume(taskId).then(() => setNotice("Monitoring resumed and the job completed."), (error) => setNotice(describeApiError(error)))} onCancel={(taskId) => void jobs.cancel(taskId).then(() => setNotice("Cancellation requested. The worker will stop at the next safe checkpoint."), (error) => setNotice(describeApiError(error)))} />
    : <><section className="metric-grid hero-metrics" aria-label="Dataset summary">{metrics.map((metric) => <div className="metric-card" key={metric.label}><span className="metric-icon"><WorkspaceIcon name={metric.icon} size={21} /></span><span className="metric-label">{metric.label}</span><strong>{metric.value.toLocaleString("en-US")}</strong><small>{metric.caption}</small></div>)}</section><DatasetWorkspace rows={workspace.rows} columns={workspace.columns} live={auth.live} progress={progress} /></>;

  return <div className="app-shell">
    <WorkspaceSidebar activeSection={activeSection} mobileOpen={mobileNav} live={auth.live} busy={combinedBusy} onNavigate={setActiveSection} onCloseMobile={closeMobileNav} onCheckApi={checkHealth} onConnect={() => setConnectionOpen(true)} onDisconnect={() => void disconnect()} />
    <main className="workspace" id="workspace-content"><header className="topbar">
      <button ref={menuButton} className="menu-button icon-button" onClick={() => setMobileNav((value) => !value)} aria-label="Open menu" aria-expanded={mobileNav} aria-controls="workspace-navigation"><WorkspaceIcon name="menu" /></button>
      <div className="breadcrumbs">Workspace <span>/</span> <strong>{sectionLabel}</strong></div>
      <div className="topbar-tools"><button className="text-button tour-trigger" onClick={() => setTourStep(0)}>Guided tour</button><button className="icon-button theme-toggle" onClick={toggleTheme} aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`} title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}><WorkspaceIcon name={theme === "dark" ? "sun" : "moon"} /></button></div>
    </header>
      <section className="dataset-heading"><div><div className="title-line"><h1>{SECTION_COPY[activeSection].title}</h1><span className={auth.live ? "connected" : "connected demo"}><span className="status-dot" />{auth.live ? "Live API" : "Demo mode"}</span></div><p>{SECTION_COPY[activeSection].subtitle}</p></div>
        <div className="top-actions"><button className="secondary" disabled={combinedBusy} onClick={() => void restoreSample()}>Use sample</button>{workspace.uploadPhase !== "idle" ? <button className="secondary" onClick={() => void workspace.cancelUpload((taskId, options) => jobs.cancel(taskId, options)).then((cancelled) => { if (cancelled) setNotice("Cancelling the active workflow…"); })}>Cancel workflow</button> : null}<label className={`primary file-button${combinedBusy ? " disabled" : ""}`} aria-disabled={combinedBusy}><WorkspaceIcon name="upload" size={17} />{uploadLabel}<input aria-label="Upload dataset file" type="file" accept=".csv,.xlsx,.xls,.json,.parquet" disabled={combinedBusy} onChange={(event) => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ""; void upload(file); }} /></label></div>
      </section>
      <div className="notice" role="status" aria-live="polite"><span aria-hidden="true">i</span>{notice}</div>
      <section className="dataset-strip" aria-label="Current dataset"><div className="dataset-identity"><span className="dataset-icon"><WorkspaceIcon name="datasets" size={18} /></span><strong title={workspace.datasetName}>{workspace.datasetName}</strong><span className="status-pill">{workspace.dataset ? `Revision ${workspace.dataset.version}` : "Local preview"}</span></div><button className="text-button" onClick={() => setActiveSection(activeSection === "transformations" ? "datasets" : "transformations")}>{activeSection === "transformations" ? "Inspect dataset" : "View transformations"}<WorkspaceIcon name="chevron" size={15} /></button></section>
      {content}
      <footer className="workspace-footer"><div className="footer-summary"><WorkspaceIcon name="datasets" size={20} /><div><strong>{rowCount.toLocaleString("en-US")} {workspace.dataset ? "rows" : "preview rows"} <span>·</span> {columnCount} columns</strong><span>{auth.live ? "Authenticated session · server persistence" : "Demo workspace · local changes only"}</span></div></div><button className="primary" disabled={combinedBusy || transformations.undoNeedsRecovery} onClick={exportDataset}><WorkspaceIcon name="download" size={17} />Export current version</button></footer>
    </main>
    {connectionOpen ? <ConnectionPanel busy={auth.busy} onClose={() => setConnectionOpen(false)} onSubmit={connect} /> : null}
    {tourStep !== null ? <div className="tour-backdrop" role="presentation"><section ref={tourDialog} tabIndex={-1} className="tour-dialog" role="dialog" aria-modal="true" aria-labelledby="tour-title"><span className="tour-progress">{tourStep + 1} of {TOUR_STEPS.length}</span><h2 id="tour-title">{TOUR_STEPS[tourStep].title}</h2><p>{TOUR_STEPS[tourStep].body}</p><div className="tour-dots" aria-hidden="true">{TOUR_STEPS.map((_, index) => <span key={index} className={index === tourStep ? "active" : ""} />)}</div><div className="button-row"><button className="secondary" onClick={() => setTourStep(null)}>Skip tour</button><button className="primary" data-dialog-initial-focus="true" onClick={() => setTourStep(tourStep === TOUR_STEPS.length - 1 ? null : tourStep + 1)}>{tourStep === TOUR_STEPS.length - 1 ? "Explore DataFlow" : "Next"}</button></div></section></div> : null}
  </div>;
}
