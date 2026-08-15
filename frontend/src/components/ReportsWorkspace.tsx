import { useEffect, useState } from "react";
import { createReport, downloadReport, fetchReportStatus, listReports, type JobStatus, type Report } from "../api-client";

type Props = { live: boolean; projectId: number | null; datasetId: number | null; datasetName: string; rows: number; columns: number; onNotice: (message: string) => void; onTrack: (taskId: string, label: string) => Promise<JobStatus> };

function saveBlob(blob: Blob, filename: string) { const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = filename; link.click(); URL.revokeObjectURL(url); }

export function ReportsWorkspace({ live, projectId, datasetId, datasetName, rows, columns, onNotice, onTrack }: Props) {
  const [reports, setReports] = useState<Report[]>([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (projectId) void listReports(projectId).then(setReports).catch(() => setReports([])); else setReports([]); }, [projectId]);

  async function generate() {
    if (!live || !projectId || !datasetId) { onNotice("Connect the API and upload a dataset to generate an audited PDF report."); return; }
    setBusy(true);
    try {
      const queued = await createReport(projectId, datasetId);
      await onTrack(queued.task_id, "PDF report");
      const result = await fetchReportStatus(queued.report_id);
      if (result.status !== "completed") throw new Error("PDF generation did not complete.");
      setReports(await listReports(projectId)); onNotice("The PDF report is ready to download.");
    } catch (error) { onNotice(error instanceof Error ? error.message : "The report could not be generated."); }
    finally { setBusy(false); }
  }

  async function download(id: number) { try { saveBlob(await downloadReport(id), `dataflow-report-${id}.pdf`); } catch (error) { onNotice(error instanceof Error ? error.message : "The report could not be downloaded."); } }

  return <section className="reports-layout"><article className="panel report-preview"><div className="report-cover"><span>DATAFLOW / ANALYTICS REPORT</span><h2>{datasetName}</h2><p>Automated profiling summary and governance evidence</p><div className="report-kpis"><div><strong>{rows}</strong><span>Rows</span></div><div><strong>{columns}</strong><span>Columns</span></div><div><strong>{new Date().toLocaleDateString("en-US")}</strong><span>Generated</span></div></div></div></article>
    <article className="panel"><div className="panel-header"><div><h2>PDF reports</h2><p>Worker-generated, auditable portfolio deliverables</p></div><button className="primary" disabled={busy || !live || !datasetId} onClick={generate}>{busy ? "Generating…" : "Generate PDF"}</button></div>
      <div className="report-list">{reports.length ? reports.map((report) => <div key={report.id}><div><strong>Report #{report.id}</strong><span>{new Date(report.created_at).toLocaleString("en-US")}</span></div><span className={`status-pill ${report.status === "completed" ? "success" : ""}`}>{report.status}</span><button className="secondary" disabled={report.status !== "completed"} onClick={() => download(report.id)}>Download</button></div>) : <div className="empty-state"><strong>No generated reports</strong><span>Connect the API, upload a dataset, and generate your first PDF.</span></div>}</div>
    </article></section>;
}
