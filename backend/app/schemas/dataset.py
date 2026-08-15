from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class DatasetResponse(BaseModel):
    id: int
    project_id: int
    original_filename: str
    status: str
    row_count: int | None
    column_count: int | None
    version: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class DatasetProfileResponse(BaseModel):
    dataset_id: int
    profile: dict[str, Any]


class PreviewResponse(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    total_rows: int
    page: int
    page_size: int


class TransformationRequest(BaseModel):
    operation: Literal["drop_columns", "rename_columns", "fill_nulls", "drop_duplicates", "cast_types"]
    parameters: dict[str, Any] = Field(default_factory=dict)
    expected_version: int = Field(ge=1)


class TransformationPreviewResponse(BaseModel):
    operation: str
    before: dict[str, Any]
    after: dict[str, Any]
    rows: list[dict[str, Any]]


class TransformationResponse(BaseModel):
    id: int
    operation: str
    parameters: dict[str, Any]
    status: str
    task_id: str | None = None
    expected_version: int
    before_rows: int
    after_rows: int
    before_columns: int
    after_columns: int
    undone_at: datetime | None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class JobResponse(BaseModel):
    task_id: str
    status: str
    progress: int = 0
    result: dict[str, Any] | None = None


class DatasetUploadJobResponse(BaseModel):
    dataset_id: int
    task_id: str
    status: str


class TransformationJobResponse(BaseModel):
    transformation_id: int
    task_id: str
    status: str
    reused: bool = False
