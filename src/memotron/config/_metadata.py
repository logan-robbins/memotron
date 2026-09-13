"""Metadata filter grammar for scoping a dream job to a subset of episodes.

A filter is a flat mapping of scalars and scalar lists; validation is strict and
up front so a malformed filter fails at config time rather than silently matching
nothing at run time."""

from __future__ import annotations

from typing import Any

MetadataFilterScalar = str | int | float | bool


MetadataFilterValue = MetadataFilterScalar | tuple[MetadataFilterScalar, ...]


def validate_metadata_filter(value: Any) -> dict[str, MetadataFilterValue]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("metadata_filter must be a dictionary")
    normalized: dict[str, MetadataFilterValue] = {}
    for raw_key, raw_expected in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError("metadata_filter keys must be non-blank strings")
        key = raw_key.strip()
        if isinstance(raw_expected, list | tuple):
            if not raw_expected:
                raise ValueError("metadata_filter array values cannot be empty")
            normalized[key] = tuple(_validate_metadata_filter_scalar(item) for item in raw_expected)
        else:
            normalized[key] = _validate_metadata_filter_scalar(raw_expected)
    return normalized


def metadata_matches_filter(metadata: object, metadata_filter: dict[str, object]) -> bool:
    if not metadata_filter:
        return True
    if not isinstance(metadata, dict):
        return False
    for key, expected in metadata_filter.items():
        if key not in metadata:
            return False
        if not _metadata_value_matches(metadata[key], expected):
            return False
    return True


def _validate_metadata_filter_scalar(value: Any) -> MetadataFilterScalar:
    if isinstance(value, str | int | float | bool):
        return value
    raise ValueError("metadata_filter values must be strings, numbers, booleans, or arrays of those scalars")


def _metadata_value_matches(actual: object, expected: object) -> bool:
    if isinstance(expected, tuple | list | set):
        return any(_metadata_value_matches(actual, option) for option in expected)
    if isinstance(actual, tuple | list | set):
        return any(item == expected for item in actual)
    return actual == expected
