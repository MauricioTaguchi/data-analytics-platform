from pydantic import BaseModel

class ReportJobResponse(BaseModel):
    report_id: int
    task_id: str
    status: str

class ReportResponse(BaseModel):
    report_id: int
    status: str
    download_url: str | None = None
