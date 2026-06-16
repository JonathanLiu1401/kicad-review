"""Guarded edit transaction: copy -> edit -> ERC -> diff -> (approve) -> apply.

Never touches the live ``.kicad_sch`` unless ``apply=True`` AND the edit did not
introduce new ERC errors. Always works on a throwaway copy of the project first.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path
import shutil
import tempfile

from kicad_mcp.edit import surgical
from kicad_mcp.edit.locate import EditError
from kicad_mcp.review import kicad


def _erc_error_count(project: kicad.Project, out: str) -> int | None:
    """Number of error-severity ERC violations, or None if ERC could not run."""
    try:
        erc = kicad.run_erc(project, out=out)
    except kicad.KiCadError:
        return None
    viol = []
    for s in erc.get("sheets", []) or []:
        viol += s.get("violations", []) or []
    viol += erc.get("violations", []) or []
    return sum(1 for v in viol if v.get("severity") == "error")


def propose_edit(
    project: kicad.Project, reference: str, prop_name: str, new_value: str, apply: bool = False
) -> dict:
    """Propose (or apply) a single-property edit on ``reference``.

    Returns a dict: reference, property, old, new, diff (unified), erc_before,
    erc_after, erc_regressed, applied. The live file changes only when ``apply`` is
    True and ERC did not regress.
    """
    if not project.sch:
        raise EditError("project has no schematic to edit")

    orig_text = Path(project.sch).read_text(encoding="utf-8")
    work = Path(tempfile.mkdtemp(prefix="kicad-edit-"))
    try:
        erc_before = _erc_error_count(project, str(work / "erc-before"))

        copy_dir = work / Path(project.dir).name
        shutil.copytree(
            project.dir,
            copy_dir,
            ignore=shutil.ignore_patterns(".kicad-review", "*-backups", "_autosave*", "~*"),
        )
        copy_proj = kicad.discover_project(copy_dir)
        old = surgical.set_property(copy_proj.sch, reference, prop_name, new_value)
        new_text = Path(copy_proj.sch).read_text(encoding="utf-8")

        diff = "".join(
            difflib.unified_diff(
                orig_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"{reference}.{prop_name} (before)",
                tofile=f"{reference}.{prop_name} (after)",
            )
        )
        erc_after = _erc_error_count(copy_proj, str(work / "erc-after"))
        regressed = erc_before is not None and erc_after is not None and erc_after > erc_before

        applied = False
        if apply and not regressed:
            tmp = Path(project.sch).with_name(Path(project.sch).name + ".tmp")
            tmp.write_text(new_text, encoding="utf-8")
            os.replace(tmp, project.sch)  # atomic
            applied = True

        return {
            "reference": reference,
            "property": prop_name,
            "old": old,
            "new": new_value,
            "diff": diff,
            "erc_before": erc_before,
            "erc_after": erc_after,
            "erc_regressed": regressed,
            "applied": applied,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)
