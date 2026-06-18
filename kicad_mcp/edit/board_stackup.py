"""Guarded write of JLCPCB's standard stackup into a ``.kicad_pcb``.

Surgically updates the EXISTING stackup's copper + dielectric layer thicknesses (and dielectric
``epsilon_r``) to JLCPCB's published reference for the board's layer-count/thickness. It does NOT
regenerate the stackup or invent layers: it requires a stackup whose copper/dielectric sequence
already matches the reference, else it refuses (a stackup write has no safe default direction).
The general (finished) thickness is left as the nominal JLCPCB target.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path
import re

import sexpdata

from kicad_mcp.edit.locate import EditError
from kicad_mcp.edit.place import _match_paren
from kicad_mcp.edit.zones import _loads_ok
from kicad_mcp.review import jlcpcb
from kicad_mcp.review.parse import _getval, parse_board


def _layer_blocks(stackup_text: str) -> list[tuple[str, int, int]]:
    """[(type, start, end)] for each copper/dielectric ``(layer ...)`` in the stackup, in order
    (silk/mask/paste layers are skipped)."""
    out: list[tuple[str, int, int]] = []
    for m in re.finditer(r"\(layer\b", stackup_text):
        start = m.start()
        end = _match_paren(stackup_text, stackup_text.index("(", start))
        typ = str(_getval(sexpdata.loads(stackup_text[start:end]), "type") or "").lower()
        if "copper" in typ:
            out.append(("copper", start, end))
        elif "prepreg" in typ:
            out.append(("prepreg", start, end))
        elif "core" in typ:
            out.append(("core", start, end))
    return out


def _set_num(layer_text: str, key: str, value: float) -> str:
    pat = re.compile(r"(\(" + key + r" )(-?[\d.]+)(\))")
    return pat.sub(lambda m: m.group(1) + f"{value:g}" + m.group(3), layer_text, count=1)


def propose_stackup(project, apply: bool = False) -> dict:
    """Propose (or apply) setting the board's stackup to JLCPCB's standard for its config.

    Returns ``{code, changes:[{layer,field,old,new}], diff, loads_ok, applied, note}``. The live
    ``.kicad_pcb`` changes only when ``apply`` is True, the edited board still loads in kicad-cli,
    and there was something to change.
    """
    if not project.pcb:
        raise EditError("project has no .kicad_pcb")
    pcb = Path(project.pcb)
    text = pcb.read_text(encoding="utf-8")

    board = parse_board(project.pcb)
    thickness = jlcpcb.board_thickness_mm(project.pcb)
    ref = jlcpcb.reference_stackup(board.copper_layers, thickness)
    if ref is None:
        raise EditError(
            f"no JLCPCB reference stackup on file for {board.copper_layers}L/{thickness} mm "
            f"(only common configs are vendored: {jlcpcb.STACKUP_SOURCE})"
        )

    sm = re.search(r"\(stackup\b", text)
    if not sm:
        raise EditError(
            "board has no (stackup ...) block to update -- set the stackup in KiCad Board Setup first"
        )
    s0, s1 = sm.start(), _match_paren(text, text.index("(", sm.start()))
    block = text[s0:s1]

    blocks = _layer_blocks(block)
    ref_layers = ref["layers"]
    if [b[0] for b in blocks] != [layer["type"] for layer in ref_layers]:
        raise EditError(
            f"board stackup sequence {[b[0] for b in blocks]} does not match JLCPCB's "
            f"{ref['code']} {[layer['type'] for layer in ref_layers]} -- set it in KiCad Board Setup"
        )

    # forward pass: collect changes for the report
    changes: list[dict] = []
    for (_typ, ls, le), rl in zip(blocks, ref_layers):
        node = sexpdata.loads(block[ls:le])
        old_th = _getval(node, "thickness")
        if old_th is not None and abs(float(old_th) - rl["thickness"]) > 1e-6:
            changes.append(
                {
                    "layer": rl["role"],
                    "field": "thickness",
                    "old": float(old_th),
                    "new": rl["thickness"],
                }
            )
        old_er = _getval(node, "epsilon_r")
        if (
            rl.get("epsilon_r")
            and old_er is not None
            and abs(float(old_er) - rl["epsilon_r"]) > 1e-6
        ):
            changes.append(
                {
                    "layer": rl["role"],
                    "field": "epsilon_r",
                    "old": float(old_er),
                    "new": rl["epsilon_r"],
                }
            )

    # reverse pass: edit spans from the end so earlier offsets stay valid
    new_block = block
    for (_typ, ls, le), rl in reversed(list(zip(blocks, ref_layers))):
        lt = new_block[ls:le]
        upd = _set_num(lt, "thickness", rl["thickness"])
        if rl.get("epsilon_r"):
            upd = _set_num(upd, "epsilon_r", rl["epsilon_r"])
        new_block = new_block[:ls] + upd + new_block[le:]

    new_text = text[:s0] + new_block + text[s1:]
    sexpdata.loads(new_text)  # structural gate

    loads_ok = True
    if changes:
        import tempfile

        tmp_dir = Path(tempfile.mkdtemp(prefix="kicad-stackup-"))
        try:
            tmp_pcb = tmp_dir / pcb.name
            tmp_pcb.write_text(new_text, encoding="utf-8")
            loads_ok = _loads_ok(tmp_pcb)
        finally:
            import shutil

            shutil.rmtree(tmp_dir, ignore_errors=True)

    diff = "".join(
        difflib.unified_diff(
            text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile="stackup (before)",
            tofile=f"stackup (after, JLCPCB {ref['code']})",
        )
    )
    applied = False
    if apply and changes and loads_ok:
        tmp = pcb.with_name(pcb.name + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, pcb)  # atomic
        applied = True

    return {
        "code": ref["code"],
        "changes": changes,
        "diff": diff,
        "loads_ok": loads_ok,
        "applied": applied,
        "note": (
            "sets the JLC04161H-7628 standard (outer 1 oz / inner 0.5 oz). The finished board "
            "thickness is left as the nominal JLCPCB target. JLCPCB assigns the final build stack "
            "at order time -- this makes KiCad's impedance match their published standard."
        ),
    }
