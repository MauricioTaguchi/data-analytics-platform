from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ReportProvenance(BaseModel):
    dataset_version: int | None = None
    snapshot_id: str | None = None
    snapshot_version: int | None = None
    snapshot_sha256: str | None = None
    snapshot_size_bytes: int | None = None
    source_filename: str | None = None


class ReportJobResponse(BaseModel):
    report_id: int
    task_id: str
    status: str
    dataset_version: int | None = None
    snapshot_sha256: str | None = None
    reused: bool = False


class ReportResponse(ReportProvenance):
    report_id: int
    status: str
    error_message: str | None = None
    download_url: str | None = None


class ReportListItem(ReportProvenance):
    id: int
    project_id: int
    dataset_id: int
    status: str
    task_id: str | None = None
    error_message: str | None = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
