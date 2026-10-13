"""Versioned transformation handlers; importing the catalog allocates no frames.

Keep released handlers unchanged. Add a new function and semantic version when
behavior changes so admitted jobs remain reproducible across deployments.
"""

from typing import Any


def _known_columns(frame: Any, names: Any) -> None:
    unknown = set(names) - set(frame.columns)
    if unknown:
        raise ValueError(f"Unknown columns: {', '.join(sorted(unknown))}")


def drop_columns_v1(frame: Any, parameters: dict) -> Any:
    columns = parameters.get("columns", [])
    _known_columns(frame, columns)
    return frame.drop(columns=columns)


def rename_columns_v1(frame: Any, parameters: dict) -> Any:
    mapping = parameters.get("mapping", {})
    _known_columns(frame, mapping)
    renamed = [mapping.get(column, column) for column in frame.columns]
    if len(renamed) != len(set(renamed)):
        raise ValueError("Renaming would create duplicate column names.")
    return frame.rename(columns=mapping)


def fill_nulls_v1(frame: Any, parameters: dict) -> Any:
    values = parameters.get("values", {})
    _known_columns(frame, values)
    return frame.fillna(values)


def drop_duplicates_v1(frame: Any, parameters: dict) -> Any:
    return frame.drop_duplicates()


def cast_types_v1(frame: Any, parameters: dict) -> Any:
    from app.services.safe_cast import safe_cast_column

    mapping = parameters.get("mapping", {})
    _known_columns(frame, mapping)
    result = frame.copy(deep=True)
    for column, dtype in mapping.items():
        result[column] = safe_cast_column(result[column], dtype)
    return result


def drop_projection_v1(columns: list[str], parameters: dict) -> list[tuple[str, str]]:
    requested = parameters.get("columns", [])
    if not isinstance(requested, list) or any(not isinstance(name, str) for name in requested):
        raise ValueError("columns must be a list of column names.")
    unknown = set(requested) - set(columns)
    if unknown:
        raise ValueError(f"Unknown columns: {', '.join(sorted(unknown))}")
    return [(name, name) for name in columns if name not in set(requested)]


def rename_projection_v1(columns: list[str], parameters: dict) -> list[tuple[str, str]]:
    mapping = parameters.get("mapping", {})
    if not isinstance(mapping, dict) or any(
        not isinstance(source, str) or not isinstance(target, str) or "\x00" in target
        for source, target in mapping.items()
    ):
        raise ValueError("mapping must map column names to valid string names.")
    unknown = set(mapping) - set(columns)
    if unknown:
        raise ValueError(f"Unknown columns: {', '.join(sorted(unknown))}")
    result = [(name, mapping.get(name, name)) for name in columns]
    renamed = [target for _, target in result]
    if len(renamed) != len(set(renamed)):
        raise ValueError("Renaming would create duplicate column names.")
    return result
