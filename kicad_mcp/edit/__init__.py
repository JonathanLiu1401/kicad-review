"""kicad_mcp.edit -- v1 surgical schematic editing.

Changes one property (Value / Footprint association / any instance field) of a placed
symbol in a ``.kicad_sch`` *in place*, via a UUID-anchored byte-span replacement -- so
the rest of the file is byte-identical and no KiCad-10 token is ever dropped (unlike a
full-file resave through a foreign serializer). Read-only locating reuses the
``kicad_mcp.review.parse`` sexpdata helpers; the write is textual.
"""

from .locate import EditError, Instance, find_instance  # noqa: F401
from .surgical import set_footprint, set_property, set_value  # noqa: F401

__all__ = [
    "EditError",
    "Instance",
    "find_instance",
    "set_property",
    "set_value",
    "set_footprint",
]
