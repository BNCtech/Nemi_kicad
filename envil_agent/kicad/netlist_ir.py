"""Reconstruct a TopologyIR from an EXISTING schematic via KiCad's own
netlister (`kicad-cli sch export netlist`).

This is the general "schematic -> board" path the `generate_pcb` tool uses when
there is no `.envil-ir.json` sidecar (e.g. a project built before the sidecar
existed, or a schematic Envil didn't author). KiCad does the connectivity
analysis, so the resulting IR carries exactly what the schematic means — no
fragile re-implementation of wire/label/power-port netlisting here.

Works WHILE the project is open in KiCad: the schematic is copied into a
throwaway temp dir first (KiCad holds a `~<name>.kicad_sch.lck` on the original
and kicad-cli hangs on the locked file — the same lock-dodge erc_check uses).

Never raises for circuit reasons — returns None when the netlist can't be
produced/parsed, so the caller can fall back gracefully.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import sexpdata

from ..intent.ir import TopologyIR, IRComponent, IRNet


def _head(node: Any) -> Optional[str]:
    if isinstance(node, list) and node:
        f = node[0]
        if isinstance(f, sexpdata.Symbol):
            return f.value()
        if isinstance(f, str):
            return f
    return None


def _kids(node: list, name: str) -> List[list]:
    return [c for c in node[1:]
            if isinstance(c, list) and _head(c) == name] if isinstance(node, list) else []


def _kid(node: list, name: str) -> Optional[list]:
    for c in _kids(node, name):
        return c
    return None


def _val(node: list, name: str) -> str:
    """String payload of a (name "value") child, e.g. (ref "R1") -> "R1"."""
    c = _kid(node, name)
    if c and len(c) >= 2:
        return str(c[1])
    return ""


def _resolve_cli() -> Optional[str]:
    """Reuse erc_check's resolver so there is one kicad-cli policy."""
    try:
        from ..intent.engine import _load_layout_config
        cfg = (_load_layout_config() or {}).get("erc_check", {}) or {}
    except Exception:
        cfg = {}
    try:
        from ..tools.erc_check import _resolve_kicad_cli
        return _resolve_kicad_cli(cfg)
    except Exception:
        return shutil.which("kicad-cli")


def _parse_netlist(text: str, fallback_name: str) -> Optional[TopologyIR]:
    try:
        root = sexpdata.loads(text)
    except Exception:
        return None
    if _head(root) != "export":
        return None

    # circuit name from the title block when present
    name = fallback_name
    design = _kid(root, "design")
    if design:
        for sheet in _kids(design, "sheet"):
            tb = _kid(sheet, "title_block")
            if tb:
                t = _val(tb, "title")
                if t:
                    name = t
                    break

    comps_node = _kid(root, "components")
    components: List[IRComponent] = []
    if comps_node:
        for comp in _kids(comps_node, "comp"):
            ref = _val(comp, "ref")
            if not ref:
                continue
            value = _val(comp, "value")
            footprint = _val(comp, "footprint")
            lib_id = ""
            ls = _kid(comp, "libsource")
            if ls:
                lib = _val(ls, "lib")
                part = _val(ls, "part")
                lib_id = f"{lib}:{part}" if lib and part else (part or "")
            components.append(IRComponent(ref=ref, lib_id=lib_id,
                                          value=value, footprint=footprint))

    nets_node = _kid(root, "nets")
    nets: List[IRNet] = []
    if nets_node:
        for net in _kids(nets_node, "net"):
            nm = _val(net, "name")
            pins: List[str] = []
            for node in _kids(net, "node"):
                nref = _val(node, "ref")
                npin = _val(node, "pin")
                if nref and npin:
                    pins.append(f"{nref}.{npin}")
            if nm and len(pins) >= 1:
                # power rails are unnamed-ish ("GND","+3V3","+5V"...) — mark so the
                # PCB-side keeps them as global nets; harmless if wrong.
                is_pwr = nm.upper() in ("GND", "GNDA", "GNDD", "VCC", "VDD") \
                    or nm.startswith("+") or nm.startswith("-")
                nets.append(IRNet(name=nm, pins=pins, is_power=is_pwr))

    if not components:
        return None
    return TopologyIR(name=name or fallback_name, circuit_type="IMPORTED",
                      components=components, nets=nets)


async def schematic_to_ir(sch_path: str, timeout: float = 60.0
                          ) -> Optional[TopologyIR]:
    """Run kicad-cli on a temp copy of the schematic and parse the netlist into
    a TopologyIR. Returns None on any failure (missing cli, locked, parse)."""
    sch = Path(sch_path)
    if not sch.exists() or sch.suffix.lower() != ".kicad_sch":
        return None
    cli = _resolve_cli()
    if not cli:
        return None

    tmpdir = Path(tempfile.mkdtemp(prefix="envil_netlist_"))
    try:
        # Copy the whole project so hierarchical sheets resolve, then run on the
        # copy (works while the original is open/locked in KiCad).
        for pat in ("*.kicad_sch", "*.kicad_pro"):
            for f in sch.parent.glob(pat):
                shutil.copy2(f, tmpdir / f.name)
        for nm in ("sym-lib-table", "fp-lib-table"):
            src = sch.parent / nm
            if src.exists():
                shutil.copy2(src, tmpdir / nm)
        target = tmpdir / sch.name
        out = tmpdir / (sch.stem + ".net")
        try:
            proc = await asyncio.create_subprocess_exec(
                cli, "sch", "export", "netlist", "--format", "kicadsexpr",
                str(target), "-o", str(out),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (asyncio.TimeoutError, FileNotFoundError, OSError):
            try:
                proc.kill()
            except Exception:
                pass
            return None
        if not out.exists():
            return None
        return _parse_netlist(out.read_text(encoding="utf-8", errors="replace"),
                              sch.stem)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
