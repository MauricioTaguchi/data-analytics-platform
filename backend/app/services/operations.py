"""Operation contracts shared by request validation, previews and admission.

Adding an operation requires its parameter schema, declared capabilities and an
engine implementation. The registry deliberately does not import a dataframe
runtime: native schema checks inspect only Parquet metadata.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictStr, ValidationError, field_validator

from app.core.config import settings
from app.services.engines.contracts import EngineCapabilityError, TransformationPlan
from app.services import operation_handlers as handlers


EngineName = Literal["pandas", "polars", "duckdb"]


def _valid_column_name(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise ValueError("Column names must be non-empty strings without null bytes.")
    if len(value) > settings.MAX_DATASET_COLUMN_NAME_CHARS:
        raise ValueError(
            f"Column names are limited to {settings.MAX_DATASET_COLUMN_NAME_CHARS:,} characters."
        )
    # Keep literal spelling, including whitespace and SQL/expression characters.
    return value


ColumnName = Annotated[StrictStr, AfterValidator(_valid_column_name)]
Scalar = str | int | float | bool


class OperationParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class DropColumnsParameters(OperationParameters):
    columns: list[ColumnName] = Field(default_factory=list)

    @field_validator("columns")
    @classmethod
    def distinct_names(cls, names: list[str]) -> list[str]:
        if len(names) != len(set(names)):
            raise ValueError("columns must not contain duplicate names.")
        return names


class RenameColumnsParameters(OperationParameters):
    mapping: dict[ColumnName, ColumnName] = Field(default_factory=dict)

    @field_validator("mapping")
    @classmethod
    def distinct_targets(cls, mapping: dict[str, str]) -> dict[str, str]:
        if len(mapping.values()) != len(set(mapping.values())):
            raise ValueError("Renaming would create duplicate column names.")
        return mapping


class FillNullsParameters(OperationParameters):
    values: dict[ColumnName, Scalar] = Field(default_factory=dict)


class DropDuplicatesParameters(OperationParameters):
    """Version 1 removes complete duplicate rows, keeping the first occurrence."""


CastType = Literal[
    "string", "str", "object", "bool", "boolean",
    "int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64",
    "Int8", "Int16", "Int32", "Int64", "UInt8", "UInt16", "UInt32", "UInt64",
    "float32", "float64", "Float32", "Float64", "datetime64[ns]", "timedelta64[ns]",
]


class CastTypesParameters(OperationParameters):
    mapping: dict[ColumnName, CastType] = Field(default_factory=dict)


@dataclass(frozen=True)
class OperationDefinition:
    name: str
    semantic_version: str
    parameters_model: type[OperationParameters]
    engines: tuple[EngineName, ...]
    preserves_rows: bool
    preserves_order: bool
    preserves_types: bool
    pandas_handler: Callable[[Any, dict], Any]
    projection_handler: Callable[[list[str], dict], list[tuple[str, str]]] | None = None


class OperationRegistry:
    """Retain released definitions and choose a default only for new admissions."""

    def __init__(self) -> None:
        self._versions: dict[tuple[str, str], OperationDefinition] = {}
        self._current: dict[str, OperationDefinition] = {}

    @property
    def current(self):
        return MappingProxyType(self._current)

    def register(self, definition: OperationDefinition, *, current: bool = True) -> None:
        key = (definition.name, definition.semantic_version)
        if key in self._versions:
            raise ValueError(f"Operation {definition.name}@{definition.semantic_version} is already registered.")
        self._versions[key] = definition
        if current:
            self._current[definition.name] = definition

    def released(self, operation: str) -> list[OperationDefinition]:
        return [definition for (name, _), definition in self._versions.items() if name == operation]

    def resolve(self, operation: str, version: str | None = None) -> OperationDefinition:
        try:
            return self._current[operation] if version is None else self._versions[(operation, version)]
        except KeyError:
            suffix = f"@{version}" if version is not None else ""
            raise ValueError(f"Unsupported transformation operation: {operation}{suffix}.") from None


OPERATION_REGISTRY = OperationRegistry()
for _definition in (
    OperationDefinition("drop_columns", "1.0.0", DropColumnsParameters, ("pandas", "polars", "duckdb"), True, True, True, handlers.drop_columns_v1, handlers.drop_projection_v1),
    OperationDefinition("rename_columns", "1.0.0", RenameColumnsParameters, ("pandas", "polars", "duckdb"), True, True, True, handlers.rename_columns_v1, handlers.rename_projection_v1),
    OperationDefinition("fill_nulls", "1.0.0", FillNullsParameters, ("pandas",), True, True, False, handlers.fill_nulls_v1),
    OperationDefinition("drop_duplicates", "1.0.0", DropDuplicatesParameters, ("pandas",), False, True, True, handlers.drop_duplicates_v1),
    OperationDefinition("cast_types", "1.0.0", CastTypesParameters, ("pandas",), True, True, False, handlers.cast_types_v1),
):
    OPERATION_REGISTRY.register(_definition)
OPERATIONS = OPERATION_REGISTRY.current

ENGINE_FORMATS = MappingProxyType({
    "pandas": (".csv", ".json", ".xls", ".xlsx", ".parquet"),
    "polars": (".parquet",),
    "duckdb": (".parquet",),
})


def operation_definition(operation: str, version: str | None = None) -> OperationDefinition:
    return OPERATION_REGISTRY.resolve(operation, version)


def validate_operation(
    operation: str, parameters: dict[str, Any], *, version: str | None = None,
) -> dict[str, Any]:
    """Reject malformed parameters without changing existing idempotency bodies."""
    definition = operation_definition(operation, version)
    validated = definition.parameters_model.model_validate(parameters)
    return validated.model_dump(exclude_unset=True)


def validate_request_operation(operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Accept released wire contracts so a pre-upgrade request can be replayed.

    Admission still validates a *new* request against the current definition.
    A historical schema is not permission to submit new work with old semantics.
    """
    current = operation_definition(operation)
    try:
        validate_operation(operation, parameters, version=current.semantic_version)
        return parameters
    except ValidationError as current_error:
        for definition in OPERATION_REGISTRY.released(operation):
            if definition.semantic_version == current.semantic_version:
                continue
            try:
                validate_operation(operation, parameters, version=definition.semantic_version)
                return parameters
            except ValidationError:
                continue
        raise current_error


def execute_operation(frame: Any, operation: str, parameters: dict, *, version: str | None = None) -> Any:
    definition = operation_definition(operation, version)
    validated = validate_operation(operation, parameters, version=definition.semantic_version)
    result = definition.pandas_handler(frame, validated)
    if result.shape[1] == 0:
        raise ValueError("A dataset must keep at least one column.")
    return result


def validate_operation_capability(
    operation: str,
    parameters: dict[str, Any],
    *,
    engine_name: str,
    input_path: Path | str,
    version: str | None = None,
) -> TransformationPlan:
    """Validate the same executable plan before preview and durable admission.

    This is a metadata preflight, not a promise of execution success. Native
    adapters still verify output types and resource limits before publication.
    """
    definition = operation_definition(operation, version)
    validated = validate_operation(operation, parameters, version=definition.semantic_version)
    if engine_name not in ENGINE_FORMATS:
        raise EngineCapabilityError(f"Unknown transformation engine: {engine_name}.")
    if engine_name not in definition.engines:
        raise EngineCapabilityError(
            f"The {engine_name} engine does not support {operation}. Choose pandas explicitly."
        )
    path = Path(input_path)
    if path.suffix.lower() not in ENGINE_FORMATS[engine_name]:
        raise EngineCapabilityError(
            f"The {engine_name} engine does not support {path.suffix or 'this'} input. "
            "Native engines require Parquet input; choose pandas for other formats."
        )
    plan = TransformationPlan(operation, validated, definition.semantic_version)
    if engine_name != "pandas":
        import pyarrow.parquet as pq

        with pq.ParquetFile(path) as source:
            schema = source.schema_arrow
        plan.validate_schema(schema, engine_name=engine_name)
        if engine_name == "duckdb":
            if any(character in str(path) for character in "*?[]"):
                raise EngineCapabilityError("DuckDB snapshots require a literal path without glob characters.")
    return plan


def operation_catalog() -> list[dict[str, Any]]:
    """Expose versioned contracts so clients can discover current capabilities."""
    return [
        {
            "operation": definition.name,
            "semantic_version": definition.semantic_version,
            "parameters_schema": definition.parameters_model.model_json_schema(),
            "engines": {name: list(ENGINE_FORMATS[name]) for name in definition.engines},
            "invariants": {
                "preserves_rows": definition.preserves_rows,
                "preserves_order": definition.preserves_order,
                "preserves_types": definition.preserves_types,
            },
        }
        for definition in OPERATIONS.values()
    ]
