"""Locate a placed symbol instance in a ``.kicad_sch`` by Reference (read-only)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import sexpdata

from kicad_mcp.review.parse import _get, _getall, _getval, _head, _sym


class EditError(RuntimeError):
    """Raised when an edit cannot be located or applied safely."""


@dataclass
class Instance:
    """A placed symbol instance (not a ``lib_symbols`` cache entry)."""

    reference: str
    uuid: str
    value: str | None
    footprint: str | None
    lib_id: str | None


def _instance_prop(node, name: str):
    for prop in _getall(node, "property"):
        if len(prop) >= 3 and _sym(prop[1]) == name:
            return _sym(prop[2])
    return None


def _placed_instances(data):
    """Yield the placed-instance ``(symbol ...)`` nodes (direct children of kicad_sch
    that carry a ``lib_id`` -- the ``lib_symbols`` cache lives nested under one
    ``(lib_symbols ...)`` child, so it is never iterated here)."""
    for node in data[1:] if isinstance(data, list) else []:
        if _head(node) == "symbol" and _get(node, "lib_id") is not None:
            yield node


def find_instance(sch_path: str | Path, reference: str) -> Instance | None:
    """Return the placed instance whose Reference == ``reference``, or ``None``."""
    data = sexpdata.loads(Path(sch_path).read_text(encoding="utf-8"))
    for node in _placed_instances(data):
        if _instance_prop(node, "Reference") == reference:
            uuid = _getval(node, "uuid")
            return Instance(
                reference=reference,
                uuid=str(uuid) if uuid is not None else "",
                value=_instance_prop(node, "Value"),
                footprint=_instance_prop(node, "Footprint"),
                lib_id=_getval(node, "lib_id"),
            )
    return None


def list_references(sch_path: str | Path) -> list[str]:
    """Every placed instance's Reference (handy for surfacing choices)."""
    data = sexpdata.loads(Path(sch_path).read_text(encoding="utf-8"))
    out = []
    for node in _placed_instances(data):
        ref = _instance_prop(node, "Reference")
        if ref:
            out.append(ref)
    return sorted(out)
