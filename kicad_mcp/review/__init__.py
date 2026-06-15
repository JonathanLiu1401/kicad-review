"""kicad_mcp.review -- the design-review engine.

Pure-Python, depends only on the standard library + ``sexpdata`` and the
``kicad-cli`` executable. Deliberately independent of FastMCP / the rest of the
``kicad_mcp`` package so it can be driven three ways with identical behavior:

  * the Bash/CI CLI shim  (``lib/kicad_review_cli.py``) -- the primary path the
    Claude Code skill uses,
  * the FastMCP tool wrappers (``kicad_mcp/tools/review_tools.py``),
  * direct import from the pytest suite.

The engine produces an *evidence package* (deterministic findings + rendered
image paths + datasheet pointers + a review rubric). The judgment layer -- fusing
those findings with the rendered images and datasheets into a prioritized review
-- lives in the Claude skill, not here, because only the LLM can *see* the images.
"""

from .report import Finding, Severity, Domain  # noqa: F401
from .engine import ReviewEngine  # noqa: F401

__all__ = ["Finding", "Severity", "Domain", "ReviewEngine"]
