from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

from app.schemas.validation import bounded_json
from app.services.operations import ColumnName


Scalar = StrictStr | StrictInt | StrictFloat | StrictBool | None


class RuleBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class NotNullRule(RuleBase):
    kind: Literal["not_null"]
    column: ColumnName


class UniqueRule(RuleBase):
    kind: Literal["unique"]
    column: ColumnName


class RangeRule(RuleBase):
    kind: Literal["range"]
    column: ColumnName
    min: StrictFloat | StrictInt | None = None
    max: StrictFloat | StrictInt | None = None

    @model_validator(mode="after")
    def ordered_bounds(self):
        if self.min is None and self.max is None:
            raise ValueError("A numeric range requires min or max.")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("Range min must not exceed max.")
        return self


class AcceptedValuesRule(RuleBase):
    kind: Literal["accepted_values"]
    column: ColumnName
    values: list[Scalar] = Field(min_length=1, max_length=1_000)


class SchemaRule(RuleBase):
    kind: Literal["schema"]
    expected_schema: dict[ColumnName, Annotated[StrictStr, Field(min_length=1, max_length=120)]] = Field(min_length=1, max_length=1_000)
    allow_extra_columns: bool = False


QualityRule = Annotated[NotNullRule | UniqueRule | RangeRule | AcceptedValuesRule | SchemaRule, Field(discriminator="kind")]


class QualityContractCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    expected_contract_version: int = Field(default=0, ge=0)
    rules: list[QualityRule] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def bounded_contract(self):
        if not self.name.strip():
            raise ValueError("Contract name must not be blank.")
        if len({rule.id for rule in self.rules}) != len(self.rules):
            raise ValueError("Rule identifiers must be distinct within a contract.")
        bounded_json({"rules": [rule.model_dump() for rule in self.rules]}, max_bytes=30_000, label="Quality contract")
        return self


class QualityEvaluationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_id: int = Field(ge=1)
    expected_version: int = Field(ge=1)


class QualityContractResponse(BaseModel):
    id: int
    dataset_id: int
    version: int
    name: str
    rules: list[dict[str, Any]]
    created_at: datetime


class QualityEvaluationResponse(BaseModel):
    id: int
    dataset_id: int
    contract_id: int
    contract_version: int
    dataset_version: int
    snapshot_sha256: str | None
    task_id: str
    status: str
    passed: bool | None
    result: dict[str, Any] | None
    error_message: str | None
    stale: bool
    created_at: datetime
    completed_at: datetime | None
