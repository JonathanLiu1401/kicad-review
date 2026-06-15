"""
KiCad MCP Server.

A Model Context Protocol (MCP) server for KiCad electronic design automation (EDA) files.
"""
# The FastMCP server is an optional import path: the review engine
# (``kicad_mcp.review``) and the Bash CLI shim run without ``fastmcp`` installed.
# Only pull in the server/context (which need fastmcp) when it is actually present,
# so ``import kicad_mcp.review`` works MCP-less AND a real breakage inside server.py
# is not masked as "fastmcp missing".
import importlib.util as _ilu

_HAS_FASTMCP = _ilu.find_spec("fastmcp") is not None

from .config import *  # noqa: F401,F403,E402

__version__ = "0.1.0"
__author__ = "Lama Al Rajih"
__description__ = "Model Context Protocol server for KiCad on Mac, Windows, and Linux"

__all__ = ["__version__", "__author__", "__description__"]

if _HAS_FASTMCP:
    from .server import *  # noqa: F401,F403,E402
    from .context import *  # noqa: F401,F403,E402
    __all__ += [
        "create_server", "add_cleanup_handler", "run_cleanup_handlers",
        "shutdown_server", "kicad_lifespan", "KiCadAppContext",
    ]
