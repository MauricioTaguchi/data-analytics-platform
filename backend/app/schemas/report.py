from datetime import datetime

from pydantic import BaseModel, ConfigDict

class ReportJobResponse(BaseModel):
    report_id: int
    task_id: str
    status: str

class ReportResponse(BaseModel):
    report_id: int
    status: str
    download_url: str | None = None


class ReportListItem(BaseModel):
    id: int
    project_id: int
    dataset_id: int
    status: str
    task_id: str | None = None
    error_message: str | None = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
