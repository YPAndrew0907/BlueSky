#!/usr/bin/env python3
from __future__ import annotations

import shutil
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from lxml import etree


PPTX_MIME_SLIDE = "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
PML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"


NSMAP = {"p": PML_NS, "r": R_NS}
CT_NSMAP = {"ct": CT_NS}
REL_NSMAP = {"rel": REL_NS}


@dataclass(frozen=True)
class StructureConfig:
    template_pptx: Path
    out_pptx: Path
    duplicate_slide_num: int = 2
    duplicate_count: int = 8


def _read_zip_xml(z: zipfile.ZipFile, name: str) -> etree._Element:
    data = z.read(name)
    return etree.fromstring(data)


def _write_zip_xml(z: zipfile.ZipFile, name: str, root: etree._Element) -> None:
    data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    z.writestr(name, data)


def _max_slide_number(z: zipfile.ZipFile) -> int:
    max_n = 0
    for name in z.namelist():
        if not name.startswith("ppt/slides/slide") or not name.endswith(".xml"):
            continue
        # ppt/slides/slide{n}.xml
        stem = Path(name).stem
        try:
            n = int(stem.replace("slide", ""))
        except ValueError:
            continue
        max_n = max(max_n, n)
    return max_n


def _next_rid(pres_rels_root: etree._Element) -> str:
    rids: list[int] = []
    for rel in pres_rels_root.findall("rel:Relationship", namespaces=REL_NSMAP):
        rid = rel.get("Id", "")
        if rid.startswith("rId"):
            try:
                rids.append(int(rid[3:]))
            except ValueError:
                continue
    nxt = (max(rids) + 1) if rids else 1
    return f"rId{nxt}"


def _next_slide_id(pres_root: etree._Element) -> int:
    sld_id_lst = pres_root.find("p:sldIdLst", namespaces=NSMAP)
    if sld_id_lst is None:
        raise RuntimeError("presentation.xml missing p:sldIdLst")
    ids: list[int] = []
    for sld_id in sld_id_lst.findall("p:sldId", namespaces=NSMAP):
        try:
            ids.append(int(sld_id.get("id", "0")))
        except ValueError:
            continue
    return (max(ids) + 1) if ids else 256


def _remove_notes_relationship(slide_rels_root: etree._Element) -> None:
    # Drop notesSlide relationship so cloned slides don't point at notesSlide{orig}.xml.
    for rel in list(slide_rels_root.findall("rel:Relationship", namespaces=REL_NSMAP)):
        if rel.get("Type") == "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide":
            slide_rels_root.remove(rel)


def duplicate_slide2_and_reorder(cfg: StructureConfig) -> None:
    cfg.out_pptx.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(cfg.template_pptx, "r") as zin:
        max_slide = _max_slide_number(zin)
        slide_src = f"ppt/slides/slide{cfg.duplicate_slide_num}.xml"
        slide_rels_src = f"ppt/slides/_rels/slide{cfg.duplicate_slide_num}.xml.rels"
        slide_src_bytes = zin.read(slide_src)
        slide_rels_root = _read_zip_xml(zin, slide_rels_src)
        _remove_notes_relationship(slide_rels_root)
        slide_rels_bytes = etree.tostring(slide_rels_root, xml_declaration=True, encoding="UTF-8", standalone=True)

        pres_root = _read_zip_xml(zin, "ppt/presentation.xml")
        pres_rels_root = _read_zip_xml(zin, "ppt/_rels/presentation.xml.rels")
        ct_root = _read_zip_xml(zin, "[Content_Types].xml")

        # Add duplicated slide parts + relationships.
        new_slide_nums = list(range(max_slide + 1, max_slide + 1 + cfg.duplicate_count))
        sld_id_lst = pres_root.find("p:sldIdLst", namespaces=NSMAP)
        if sld_id_lst is None:
            raise RuntimeError("presentation.xml missing p:sldIdLst")

        next_slide_id = _next_slide_id(pres_root)
        for n in new_slide_nums:
            # presentation.xml.rels entry
            rid = _next_rid(pres_rels_root)
            rel_el = etree.Element(f"{{{REL_NS}}}Relationship")
            rel_el.set("Id", rid)
            rel_el.set("Type", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide")
            rel_el.set("Target", f"slides/slide{n}.xml")
            pres_rels_root.append(rel_el)

            # presentation.xml slide id list entry
            sld_id_el = etree.Element(f"{{{PML_NS}}}sldId")
            sld_id_el.set("id", str(next_slide_id))
            sld_id_el.set(f"{{{R_NS}}}id", rid)
            sld_id_lst.append(sld_id_el)
            next_slide_id += 1

            # [Content_Types].xml override
            override = etree.Element(f"{{{CT_NS}}}Override")
            override.set("PartName", f"/ppt/slides/slide{n}.xml")
            override.set("ContentType", PPTX_MIME_SLIDE)
            ct_root.append(override)

        # Reorder slides into narrative order (by slide part number).
        # Originals 1–18 plus duplicates 19–26 inserted for bullet slides.
        desired_nums = [
            1,
            2,
            4,
            14,
            18,
            19,
            20,
            21,
            3,
            13,
            15,
            6,
            7,
            8,
            9,
            10,
            11,
            12,
            5,
            22,
            23,
            16,
            24,
            25,
            26,
            17,
        ]

        # Build rid -> target map for slides.
        rid_to_target: dict[str, str] = {}
        for rel in pres_rels_root.findall("rel:Relationship", namespaces=REL_NSMAP):
            if rel.get("Type") != "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide":
                continue
            rid = rel.get("Id")
            tgt = rel.get("Target")
            if rid and tgt:
                rid_to_target[rid] = tgt

        # Build target -> sldId element map.
        target_to_sld_id: dict[str, etree._Element] = {}
        for sld_id in list(sld_id_lst.findall("p:sldId", namespaces=NSMAP)):
            rid = sld_id.get(f"{{{R_NS}}}id")
            tgt = rid_to_target.get(rid or "")
            if tgt:
                target_to_sld_id[tgt] = sld_id

        # Clear and re-append in desired order.
        for child in list(sld_id_lst):
            sld_id_lst.remove(child)

        for n in desired_nums:
            target = f"slides/slide{n}.xml"
            el = target_to_sld_id.get(target)
            if el is None:
                raise RuntimeError(f"Missing slide target in presentation: {target}")
            sld_id_lst.append(deepcopy(el))

        # Write new PPTX.
        tmp_path = cfg.out_pptx.with_suffix(".tmp.pptx")
        if tmp_path.exists():
            tmp_path.unlink()

        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename in {"ppt/presentation.xml", "ppt/_rels/presentation.xml.rels", "[Content_Types].xml"}:
                    continue
                zout.writestr(item, zin.read(item.filename))

            # Add duplicated slide parts.
            for n in new_slide_nums:
                zout.writestr(f"ppt/slides/slide{n}.xml", slide_src_bytes)
                zout.writestr(f"ppt/slides/_rels/slide{n}.xml.rels", slide_rels_bytes)

            _write_zip_xml(zout, "ppt/presentation.xml", pres_root)
            _write_zip_xml(zout, "ppt/_rels/presentation.xml.rels", pres_rels_root)
            _write_zip_xml(zout, "[Content_Types].xml", ct_root)

        shutil.move(tmp_path, cfg.out_pptx)


def main() -> None:
    cfg = StructureConfig(
        template_pptx=Path("Slides/PPTXWT2/bluesky-data-collection-pipeline-blackboard-animated-run-20260201.pptx"),
        out_pptx=Path("Slides/Story1/Story1_Working.pptx"),
    )
    duplicate_slide2_and_reorder(cfg)
    print(f"Wrote {cfg.out_pptx}")


if __name__ == "__main__":
    main()

