#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path

from lxml import etree


PML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"


def _find_main_seq(root: etree._Element) -> etree._Element:
    node = root.xpath(
        ".//*[local-name()='timing']//*[local-name()='cTn'][@nodeType='mainSeq']/*[local-name()='childTnLst']"
    )
    if not node:
        raise ValueError("Missing mainSeq/childTnLst")
    return node[0]


def _par_by_spid(main_seq: etree._Element) -> dict[int, etree._Element]:
    out: dict[int, etree._Element] = {}
    for par in main_seq.xpath("./*[local-name()='par']"):
        sp = par.xpath(".//*[local-name()='spTgt'][@spid]")
        if not sp:
            continue
        out[int(sp[0].get("spid"))] = par
    return out


def _set_node_type(par: etree._Element, node_type: str) -> None:
    ctn = par.xpath(".//*[local-name()='cTn'][@nodeType][1]")
    if not ctn:
        raise ValueError("Missing cTn@nodeType")
    ctn[0].set("nodeType", node_type)


def _tune_slide1(root: etree._Element) -> None:
    main_seq = _find_main_seq(root)
    by_spid = _par_by_spid(main_seq)

    for spid in [199, 200, 203]:
        if spid in by_spid:
            _set_node_type(by_spid[spid], "afterEffect")

    for spid in [222, 223]:
        if spid in by_spid:
            _set_node_type(by_spid[spid], "afterEffect")

    for spid in [227, 228]:
        if spid in by_spid:
            _set_node_type(by_spid[spid], "afterEffect")


def _tune_slide2(root: etree._Element) -> None:
    main_seq = _find_main_seq(root)
    by_spid = _par_by_spid(main_seq)

    for spid in [12, 13, 14]:
        if spid in by_spid:
            _set_node_type(by_spid[spid], "afterEffect")

    if 24 in by_spid:
        _set_node_type(by_spid[24], "clickEffect")
    for spid in [25, 26]:
        if spid in by_spid:
            _set_node_type(by_spid[spid], "afterEffect")

    if 225 in by_spid:
        _set_node_type(by_spid[225], "clickEffect")
    if 226 in by_spid:
        _set_node_type(by_spid[226], "afterEffect")


def main() -> None:
    parser = argparse.ArgumentParser(description="Tune IFX enhanced 2-slide click groupings.")
    parser.add_argument("pptx_path", type=Path)
    args = parser.parse_args()

    pptx_path = args.pptx_path.resolve()
    if not pptx_path.exists():
        raise SystemExit(f"Missing PPTX: {pptx_path}")

    with zipfile.ZipFile(pptx_path, "r") as zin:
        parts = {info.filename: zin.read(info.filename) for info in zin.infolist()}

    for slide_num, tuner in [(1, _tune_slide1), (2, _tune_slide2)]:
        slide_part = f"ppt/slides/slide{slide_num}.xml"
        root = etree.fromstring(parts[slide_part])
        tuner(root)
        parts[slide_part] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

    with zipfile.ZipFile(pptx_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name, data in parts.items():
            zout.writestr(name, data)

    print(f"OK: tuned clicks in {pptx_path}")


if __name__ == "__main__":
    main()
