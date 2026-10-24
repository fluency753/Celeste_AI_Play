"""Effort identifiers are supplied by the model catalog, never by a global enum."""
import re


def valid_effort(value):
    # Transport validation only. The runtime checks membership in the selected model.
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value) is not None


def normalize_efforts(values):
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(value for value in values if valid_effort(value)))
