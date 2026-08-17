import json
from typing import Any


def bounded_json(value: dict[str, Any], *, max_bytes: int, label: str) -> dict[str, Any]:
    """Reject oversized JSON objects before they reach durable storage."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} must not exceed {max_bytes} bytes.")
    return value
