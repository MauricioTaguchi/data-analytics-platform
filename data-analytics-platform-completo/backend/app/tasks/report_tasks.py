from pathlib import Path
from app.db.session import SessionLocal
from app.models.report import Report
from app.models.project import Project
from app.models.dataset import Dataset
from app.services.report_service import ReportService
from app.worker import celery_app

@celery_app.task(name="reports.generate")
def generate_report_task(report_id: int):
    db = SessionLocal()
    try:
        report = db.query(Report).filter(Report.id == report_id).first()
        if not report:
            return {"status": "not_found"}

        report.status = "processing"
        db.commit()

        project = db.query(Project).filter(Project.id == report.project_id).first()
        dataset = db.query(Dataset).filter(Dataset.id == report.dataset_id).first()

        output_dir = Path("data/reports")
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"report-{report.id}.pdf"

        ReportService.generate_pdf(project, dataset, output_path)
        report.status = "completed"
        report.file_path = str(output_path)
        db.commit()
        return {"status": "completed", "report_id": report.id}
    except Exception as exc:
        if "report" in locals() and report:
            report.status = "failed"
            db.commit()
        return {"status": "failed", "error": str(exc)}
    finally:
        db.close()
