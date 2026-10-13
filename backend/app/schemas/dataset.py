from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.validation import bounded_json
from app.services.operations import EngineName, validate_request_operation


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
    total_columns: int
    columns_truncated: int
    page: int
    page_size: int


class TransformationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    expected_version: int = Field(ge=1)
    engine: EngineName | None = None

    @field_validator("parameters")
    @classmethod
    def validate_parameters_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        return bounded_json(value, max_bytes=20_000, label="Transformation parameters")

    @model_validator(mode="after")
    def validate_operation_parameters(self) -> "TransformationRequest":
        self.parameters = validate_request_operation(self.operation, self.parameters)
        return self


class TransformationPreviewResponse(BaseModel):
    operation: str
    before: dict[str, Any]
    after: dict[str, Any]
    rows: list[dict[str, Any]]


class UndoRequest(BaseModel):
    expected_version: int = Field(ge=1)


class TransformationResponse(BaseModel):
    id: int
    operation: str
    operation_version: str = "1.0.0"
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


class UndoResponse(TransformationResponse):
    correlation_id: str | None = None
    event_id: str
    previous_version: int
    result_version: int

    snapshot_integrity: str = "unverified"
    snapshot_id: str | None = None


class DatasetEventResponse(BaseModel):
    id: str
    kind: str
    transformation_id: int
    actor_id: int
    previous_version: int
    result_version: int
    operation_version: str
    result_json: dict[str, Any]
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class JobResponse(BaseModel):
    task_id: str
    correlation_id: str | None = None
    status: str
    progress: int = 0
    stage: str | None = None
    error_message: str | None = None
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
