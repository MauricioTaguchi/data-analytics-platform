from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.validation import bounded_json

class DashboardCreate(BaseModel):
    project_id: int = Field(ge=1)
    name: str = Field(min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=2_000)
    layout_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("layout_json")
    @classmethod
    def validate_layout_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        return bounded_json(value, max_bytes=50_000, label="Dashboard layout")

class DashboardResponse(DashboardCreate):
    id: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class ChartCreate(BaseModel):
    dataset_id: int = Field(ge=1)
    title: str = Field(min_length=2, max_length=160)
    chart_type: Literal["bar", "line", "pie", "histogram", "scatter", "table", "kpi"]
    x_column: str | None = Field(default=None, max_length=160)
    y_column: str | None = Field(default=None, max_length=160)
    aggregation: Literal["sum", "mean", "count", "min", "max"] | None = None
    filters_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("filters_json")
    @classmethod
    def validate_filters_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        return bounded_json(value, max_bytes=20_000, label="Chart filters")

class ChartResponse(ChartCreate):
    id: int
    dashboard_id: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class ChartDataResponse(BaseModel):
    labels: list[Any]
    values: list[Any]
    rows: list[dict[str, Any]] = Field(default_factory=list)
