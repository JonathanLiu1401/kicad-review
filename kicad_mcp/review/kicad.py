"""kicad-cli location, project discovery, and headless runners.

Everything that shells out to ``kicad-cli`` lives here. The review engine never
calls ``kicad-cli`` directly -- it goes through these typed helpers so the
subprocess details (paths, JSON parsing, timeouts, Windows quoting) are in one
place and easy to test.
"""

from __future__ import annotations

import glob
import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 300


class KiCadError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# kicad-cli location
# --------------------------------------------------------------------------- #
def _candidate_cli_paths() -> list[str]:
    """Common install locations, newest version first."""
    sysname = platform.system()
    out: list[str] = []
    if sysname == "Windows":
        roots = [r"C:\Program Files\KiCad", r"C:\Program Files (x86)\KiCad"]
        for root in roots:
            # versioned dirs like 10.0, 9.0 -> sort by NUMERIC version descending
            # (lexical sort is wrong: "9.0" > "10.0" as strings, which would pick KiCad 9).
            try:
                vers = sorted(
                    os.listdir(root),
                    key=lambda v: tuple(int(x) for x in re.findall(r"\d+", v)) or (0,),
                    reverse=True,
                )
            except OSError:
                vers = []
            for v in vers:
                out.append(os.path.join(root, v, "bin", "kicad-cli.exe"))
    elif sysname == "Darwin":
        out += sorted(
            glob.glob("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
            reverse=True,
        )
        out.append("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
    else:  # Linux
        out += ["/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli"]
    return out


def find_kicad_cli() -> str:
    """Return a usable kicad-cli path or raise KiCadError.

    Order: ``KICAD_CLI_PATH`` env, then ``PATH``, then common install dirs
    (newest version first).
    """
    env = os.environ.get("KICAD_CLI_PATH")
    if env and Path(env).is_file():
        return env
    name = "kicad-cli.exe" if platform.system() == "Windows" else "kicad-cli"
    on_path = shutil.which(name)
    if on_path:
        return on_path
    for cand in _candidate_cli_paths():
        if Path(cand).is_file():
            return cand
    raise KiCadError(
        "kicad-cli not found. Install KiCad 9+ or set KICAD_CLI_PATH to the "
        "kicad-cli executable."
    )


def cli_version(cli: str | None = None) -> str:
    cli = cli or find_kicad_cli()
    r = subprocess.run([cli, "version"], capture_output=True, text=True, timeout=30)
    lines = (r.stdout or r.stderr or "").strip().splitlines()
    return lines[0] if lines else "?"


def _run(args: list[str], timeout: int = DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


# --------------------------------------------------------------------------- #
# project discovery
# --------------------------------------------------------------------------- #
@dataclass
class Project:
    name: str
    dir: Path
    pro: Path | None
    sch: Path | None
    pcb: Path | None

    def exists(self) -> bool:
        return bool(self.sch or self.pcb)


def discover_project(path: str | os.PathLike) -> Project:
    """Resolve a directory or any KiCad file into a Project bundle.

    Accepts a ``.kicad_pro`` / ``.kicad_sch`` / ``.kicad_pcb`` file, or a
    directory containing exactly one project. Prefers files that share the
    ``.kicad_pro`` stem; otherwise falls back to the lone sch/pcb in the dir.
    """
    def _real(paths):
        # drop KiCad autosave/backup siblings (_autosave-*, ~*) so we never pick a
        # stale autosave as the canonical file.
        return [c for c in paths if not c.name.startswith(("_autosave", "~"))]

    p = Path(path).expanduser().resolve()
    if p.is_file():
        directory = p.parent
        stem = p.stem
    else:
        directory = p
        pros = _real(sorted(directory.glob("*.kicad_pro")))
        if pros:
            stem = pros[0].stem
        else:
            schs = _real(sorted(directory.glob("*.kicad_sch")))
            pcbs = _real(sorted(directory.glob("*.kicad_pcb")))
            stem = (schs or pcbs)[0].stem if (schs or pcbs) else directory.name

    def pick(ext: str) -> Path | None:
        exact = directory / f"{stem}{ext}"
        if exact.is_file():
            return exact
        # ignore KiCad autosave/backup siblings
        cands = [
            c for c in directory.glob(f"*{ext}")
            if not c.name.startswith(("_autosave", "~"))
        ]
        return cands[0] if cands else None

    proj = Project(
        name=stem,
        dir=directory,
        pro=pick(".kicad_pro"),
        sch=pick(".kicad_sch"),
        pcb=pick(".kicad_pcb"),
    )
    if not proj.exists():
        raise KiCadError(f"No .kicad_sch or .kicad_pcb found at {path}")
    return proj


def workdir(project: Project, out: str | None = None) -> Path:
    d = Path(out).expanduser().resolve() if out else project.dir / ".kicad-review"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
# validation runners (JSON)
# --------------------------------------------------------------------------- #
def run_erc(project: Project, out: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict:
    if not project.sch:
        raise KiCadError("No schematic to run ERC on.")
    cli = find_kicad_cli()
    dest = workdir(project, out) / "erc.json"
    r = _run(
        [cli, "sch", "erc", "--format", "json", "--severity-all",
         "--output", str(dest), str(project.sch)],
        timeout=timeout,
    )
    if not dest.is_file():
        raise KiCadError(f"ERC produced no report. stderr: {r.stderr or r.stdout}")
    return json.loads(dest.read_text(encoding="utf-8"))


def run_drc(
    project: Project, out: str | None = None, parity: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    if not project.pcb:
        raise KiCadError("No PCB to run DRC on.")
    cli = find_kicad_cli()
    dest = workdir(project, out) / "drc.json"
    args = [cli, "pcb", "drc", "--format", "json", "--severity-all"]
    if parity:
        args.append("--schematic-parity")
    args += ["--output", str(dest), str(project.pcb)]
    r = _run(args, timeout=timeout)
    if not dest.is_file():
        raise KiCadError(f"DRC produced no report. stderr: {r.stderr or r.stdout}")
    return json.loads(dest.read_text(encoding="utf-8"))


def export_netlist(project: Project, out: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> Path:
    if not project.sch:
        raise KiCadError("No schematic to export a netlist from.")
    cli = find_kicad_cli()
    dest = workdir(project, out) / f"{project.name}.net"
    _run([cli, "sch", "export", "netlist", "--output", str(dest), str(project.sch)], timeout=timeout)
    if not dest.is_file():
        raise KiCadError("Netlist export produced no file.")
    return dest


def export_bom(project: Project, out: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> Path:
    if not project.sch:
        raise KiCadError("No schematic to export a BOM from.")
    cli = find_kicad_cli()
    dest = workdir(project, out) / f"{project.name}-bom.csv"
    _run([cli, "sch", "export", "bom", "--output", str(dest), str(project.sch)], timeout=timeout)
    return dest


# --------------------------------------------------------------------------- #
# render runners (images the skill will Read)
# --------------------------------------------------------------------------- #
def render_schematic_pdf(project: Project, out: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> Path:
    if not project.sch:
        raise KiCadError("No schematic to render.")
    cli = find_kicad_cli()
    dest = workdir(project, out) / f"{project.name}-sch.pdf"
    _run([cli, "sch", "export", "pdf", "--output", str(dest), str(project.sch)], timeout=timeout)
    if not dest.is_file():
        raise KiCadError("Schematic PDF render produced no file.")
    return dest


# layer presets useful for review (front, back, power-focused)
BOARD_PRESETS: dict[str, list[str]] = {
    "front": ["F.Cu", "F.Silkscreen", "Edge.Cuts"],
    "back": ["B.Cu", "B.Silkscreen", "Edge.Cuts"],
    "copper": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu", "Edge.Cuts"],
    "all": ["F.Cu", "B.Cu", "F.Silkscreen", "Edge.Cuts"],
}


def render_board_pdf(
    project: Project, preset: str = "all", layers: list[str] | None = None,
    out: str | None = None, timeout: int = DEFAULT_TIMEOUT,
) -> Path:
    if not project.pcb:
        raise KiCadError("No PCB to render.")
    cli = find_kicad_cli()
    lyrs = layers or BOARD_PRESETS.get(preset, BOARD_PRESETS["all"])
    dest = workdir(project, out) / f"{project.name}-pcb-{preset}.pdf"
    _run(
        [cli, "pcb", "export", "pdf", "--layers", ",".join(lyrs),
         "--output", str(dest), str(project.pcb)],
        timeout=timeout,
    )
    if not dest.is_file():
        raise KiCadError("Board PDF render produced no file.")
    return dest


def render_3d(project: Project, out: str | None = None, side: str = "top", timeout: int = DEFAULT_TIMEOUT) -> Path:
    if not project.pcb:
        raise KiCadError("No PCB to render.")
    cli = find_kicad_cli()
    dest = workdir(project, out) / f"{project.name}-3d-{side}.png"
    args = [cli, "pcb", "render", "--output", str(dest)]
    if side == "bottom":
        args += ["--side", "bottom"]
    args.append(str(project.pcb))
    _run(args, timeout=timeout)
    if not dest.is_file():
        raise KiCadError("3D render produced no file.")
    return dest
