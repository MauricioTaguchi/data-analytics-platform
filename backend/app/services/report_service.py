from io import BufferedRandom
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Protocol
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.storage_service import storage

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from app.models.dataset import Dataset
    from app.models.project import Project
    from app.models.report import Report


class ProjectView(Protocol):
    @property
    def name(self) -> str: ...


class DatasetReportView(Protocol):
    @property
    def stored_path(self) -> str: ...

    @property
    def original_filename(self) -> str: ...

    @property
    def profile_json(self) -> dict[str, Any] | None: ...


class ReportSizeLimitError(ValueError):
    """Raised before a generated PDF can exceed its configured size."""


class LimitedPdfWriter(BufferedRandom):
    """Seekable ReportLab destination with continuous size and disk checks."""

    def __init__(self, path: Path, max_bytes: int) -> None:
        self._path = path
        self._max_bytes = max(1, max_bytes)
        self._high_water_mark = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        raw_file = path.open("x+b", buffering=0)
        try:
            super().__init__(raw_file)
        except Exception:
            raw_file.close()
            raise

    def write(self, data) -> int:
        projected_size = max(self._high_water_mark, self.tell() + len(data))
        if projected_size > self._max_bytes:
            raise ReportSizeLimitError(
                "The generated PDF exceeds the configured report size limit."
            )
        growth = max(0, projected_size - self._high_water_mark)
        storage.ensure_path_capacity(self._path, growth)
        written = super().write(data)
        self._high_water_mark = max(self._high_water_mark, self.tell())
        return written

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            if exc_type is not None:
                self._path.unlink(missing_ok=True)


def escape_pdf_text(value: object) -> str:
    """Escape dynamic text before ReportLab parses it as paragraph markup."""
    return escape(str(value))


class ReportService:
    @staticmethod
    def capture_input(db: "Session", *, dataset: "Dataset", project: "Project", task_id: str) -> "Report":
        """Called under owner/dataset locks, in the job/outbox transaction."""
        from app.models.report import Report
        from app.services.snapshot_service import SnapshotService

        manifest = SnapshotService.manifest(db, dataset.id, dataset.stored_path, lock=True)
        if manifest is not None and manifest.availability != "AVAILABLE":
            raise ValueError("The dataset snapshot is unavailable for reporting.")
        return Report(
            project_id=project.id, dataset_id=dataset.id, task_id=task_id, status="queued",
            dataset_version=dataset.version, snapshot_path=str(Path(dataset.stored_path).absolute()),
            snapshot_id=manifest.id if manifest else None,
            snapshot_version=manifest.version if manifest else None,
            snapshot_sha256=manifest.sha256 if manifest else None,
            snapshot_size_bytes=manifest.size_bytes if manifest else None,
            source_filename=dataset.original_filename, source_project_name=project.name,
        )

    @staticmethod
    def generate_pdf(project: ProjectView, dataset: DatasetReportView, output_path: Path | BinaryIO) -> None:
        profile = dataset.profile_json
        if profile is None:
            raise ValueError("A supervised profile is required before rendering a report.")
        styles = getSampleStyleSheet()
        destination = output_path if hasattr(output_path, "write") else str(output_path)
        document = SimpleDocTemplate(destination, pagesize=A4)
        story = [
            Paragraph("Data Analytics Platform", styles["Title"]),
            Paragraph(f"Project: {escape_pdf_text(project.name)}", styles["Heading2"]),
            Paragraph(
                f"Dataset: {escape_pdf_text(dataset.original_filename)}",
                styles["Normal"],
            ),
            Spacer(1, 12),
            Paragraph("Source provenance", styles["Heading2"]),
            Paragraph(
                f"Dataset revision: {escape_pdf_text(getattr(dataset, 'dataset_version', None) or 'Unknown (legacy source)')}",
                styles["Normal"],
            ),
            Paragraph(
                f"Snapshot version: {escape_pdf_text(getattr(dataset, 'snapshot_version', None) or 'Not recorded')}",
                styles["Normal"],
            ),
            Paragraph(
                f"Snapshot ID: {escape_pdf_text(getattr(dataset, 'snapshot_id', None) or 'Not recorded')}",
                styles["Normal"],
            ),
            Paragraph(
                f"Source SHA-256: {escape_pdf_text(getattr(dataset, 'snapshot_sha256', None) or 'Unverified: no original digest was recorded')}",
                styles["Normal"],
            ),
            Spacer(1, 12),
            Paragraph("Executive summary", styles["Heading2"]),
        ]

        summary = profile["summary"]
        summary_table = [
            ["Metric", "Value"],
            ["Rows", summary["rows"]],
            ["Columns", summary["columns"]],
            ["Duplicates", summary["duplicate_rows"]],
            ["Missing data", f"{summary['missing_percentage']}%"],
        ]
        table = Table(summary_table)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("PADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.extend([table, Spacer(1, 18), Paragraph("Column quality", styles["Heading2"])])

        rows = [["Column", "Type", "% Missing", "Unique", "Outliers"]]
        for item in profile["columns"][:25]:
            rows.append(
                [
                    Paragraph(escape_pdf_text(item["name"]), styles["BodyText"]),
                    Paragraph(escape_pdf_text(item["dtype"]), styles["BodyText"]),
                    item["missing_percentage"],
                    item["unique_count"],
                    item.get("outlier_count", 0),
                ]
            )
        column_table = Table(rows, repeatRows=1)
        column_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.extend([column_table, Spacer(1, 18), Paragraph("Recommendations", styles["Heading2"])])
        for suggestion in profile.get("suggestions", [])[:50]:
            story.append(Paragraph(f"- {escape_pdf_text(suggestion)}", styles["BodyText"]))

        document.build(story)
