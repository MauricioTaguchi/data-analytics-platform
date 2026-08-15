from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.models.dataset import Dataset
from app.models.project import Project
from app.services.dataset_service import DatasetService


class ReportService:
    @staticmethod
    def generate_pdf(project: Project, dataset: Dataset, output_path: Path) -> None:
        profile = dataset.profile_json or DatasetService.build_profile(dataset)
        styles = getSampleStyleSheet()
        document = SimpleDocTemplate(str(output_path), pagesize=A4)
        story = [
            Paragraph("Data Analytics Platform", styles["Title"]),
            Paragraph(f"Project: {project.name}", styles["Heading2"]),
            Paragraph(f"Dataset: {dataset.original_filename}", styles["Normal"]),
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
                    item["name"],
                    item["dtype"],
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
        for suggestion in profile.get("suggestions", []):
            story.append(Paragraph(f"- {suggestion}", styles["BodyText"]))

        document.build(story)
