from typing import Any, Dict, Optional


def apply_transform(value: str, transform: str, params: Optional[Dict[str, Any]] = None) -> str:
    """Apply a named transform to a field value."""
    params = params or {}

    if transform == "ZERO_PAD_LEFT":
        length = int(params.get("length", 8))
        return value.zfill(length)

    if transform == "SET_VALUE":
        return str(params.get("value", ""))

    if transform == "REMOVE_HYPHENS":
        return value.replace("-", "")

    if transform == "UPPERCASE":
        return value.upper()

    if transform == "LOWERCASE":
        return value.lower()

    if transform == "MAP_CODE":
        mapping = params.get("map", {})
        default = params.get("default", value)
        return mapping.get(value, str(default))

    if transform == "DATE_REFORMAT":
        # YYYYMMDD and CCYYMMDD are identical for years 2000+
        return value

    # Unknown transform — return unchanged
    return value
