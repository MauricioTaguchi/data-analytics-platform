import { useEffect, useState } from "react";
import { createReport, deleteReport, describeApiError, downloadReport, fetchReportStatus, isAmbiguousJobCreationError, isRequestCancelled, listReports, reconcileCreatedJob, type JobStatus, type Report } from "../api-client";

type Props = { live: boolean; projectId: number | null; datasetId: number | null; datasetName: string; rows: number; columns: number; externalBusy: boolean; onNotice: (message: string) => void; onTrack: (taskId: string, label: string) => Promise<JobStatus>; onBusyChange: (busy: boolean) => void };

function saveBlob(blob: Blob, filename: string) { const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = filename; link.click(); URL.revokeObjectURL(url); }

function reportIdentifier(result: Record<string, unknown> | null | undefined) {
  const value = Number(result?.report_id);
  return Number.isInteger(value) && value > 0 ? value : null;
}

export function ReportsWorkspace({ live, projectId, datasetId, datasetName, rows, columns, externalBusy, onNotice, onTrack, onBusyChange }: Props) {
  const [reports, setReports] = useState<Report[]>([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    if (projectId) {
      void listReports(projectId, controller.signal)
        .then(setReports)
        .catch((error) => { if (!isRequestCancelled(error)) setReports([]); });
    } else {
      setReports([]);
    }
    return () => controller.abort();
  }, [projectId]);

  async function generate() {
    if (!live || !projectId || !datasetId) { onNotice("Connect the API and upload a dataset to generate a data-quality PDF report."); return; }
    setBusy(true);
    onBusyChange(true);
    try {
      const taskId = crypto.randomUUID();
      let trackingId = taskId;
      let reportId: number | null = null;
      try {
        const queued = await createReport(projectId, datasetId, taskId);
        trackingId = queued.task_id;
        reportId = queued.report_id;
      } catch (error) {
        if (!isAmbiguousJobCreationError(error)) throw error;
        const recovered = await reconcileCreatedJob(taskId);
        if (!recovered) throw error;
      }
      const completed = await onTrack(trackingId, "PDF report");
      reportId ??= reportIdentifier(completed.result);
      if (!reportId) throw new Error("The completed report job did not identify its report.");
      const result = await fetchReportStatus(reportId);
      if (result.status !== "completed") throw new Error("PDF generation did not complete.");
      setReports(await listReports(projectId)); onNotice("The PDF report is ready to download.");
    } catch (error) { onNotice(describeApiError(error)); }
    finally { setBusy(false); onBusyChange(false); }
  }

  async function download(id: number) { try { saveBlob(await downloadReport(id), `dataflow-report-${id}.pdf`); } catch (error) { onNotice(describeApiError(error)); } }

  async function remove(id: number) {
    if (!projectId) return;
    setBusy(true); onBusyChange(true);
    try { await deleteReport(id); setReports(await listReports(projectId)); onNotice("The report was deleted and its storage was released."); }
    catch (error) { onNotice(describeApiError(error)); }
    finally { setBusy(false); onBusyChange(false); }
  }

  return <section className="reports-layout"><article className="panel report-preview"><div className="report-cover"><span>DATAFLOW / ANALYTICS REPORT</span><h2>{datasetName}</h2><p>Automated profiling summary and data-quality evidence</p><div className="report-kpis"><div><strong>{rows}</strong><span>Rows</span></div><div><strong>{columns}</strong><span>Columns</span></div><div><strong>{new Date().toLocaleDateString("en-US")}</strong><span>Generated</span></div></div></div></article>
  <article className="panel"><div className="panel-header"><div><h2>PDF reports</h2><p>Worker-generated data-quality deliverables</p></div><button className="primary" disabled={busy || externalBusy || !live || !datasetId} onClick={generate}>{busy ? "Generating…" : "Generate PDF"}</button></div>
      <div className="report-list">{reports.length ? reports.map((report) => <div key={report.id}><div><strong>Report #{report.id}</strong><span>{new Date(report.created_at).toLocaleString("en-US")}</span></div><span className={`status-pill ${report.status === "completed" ? "success" : ""}`}>{report.status}</span><div className="report-actions"><button className="secondary" disabled={busy || report.status !== "completed"} onClick={() => download(report.id)}>Download</button><button className="secondary" disabled={busy || report.status === "queued" || report.status === "processing"} onClick={() => remove(report.id)}>Delete</button></div></div>) : <div className="empty-state"><strong>No generated reports</strong><span>Connect the API, upload a dataset, and generate your first PDF.</span></div>}</div>
    </article></section>;
}
