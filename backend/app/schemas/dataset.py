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
    operation: Literal[
        "drop_columns",
        "rename_columns",
        "fill_nulls",
        "drop_duplicates",
        "cast_types",
    ]
    parameters: dict[str, Any] = Field(default_factory=dict)

class JobResponse(BaseModel):
    task_id: str
    status: str
