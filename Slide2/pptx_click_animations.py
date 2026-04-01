#!/usr/bin/env python3
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Mapping, Sequence, TypeAlias

from lxml import etree


PML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

SlideKind = Literal[
    "COVER",
    "SYSTEM",
    "UI_SCREENSHOT",
    "RQS",
    "ARTIFACTS",
    "RECEIPT",
    "CREDIBILITY",
    "NEXT",
    "APPENDIX",
    "BACKUP_API",
    "BACKUP_FOLDER",
    "BACKUP",
    "UNKNOWN",
]

TargetRef: TypeAlias = int | str
StructuredSteps: TypeAlias = list[list[TargetRef]]


_SLIDE_XML_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")


@dataclass(frozen=True)
class InjectResult:
    pptx_in: Path
    pptx_out: Path
    slide_click_effects: dict[int, int]

    @property
    def total_click_effects(self) -> int:
        return sum(self.slide_click_effects.values())


def _qn(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _extract_slide_text(slide_root: etree._Element) -> str:
    ns = {"a": A_NS}
    tokens = []
    for node in slide_root.xpath(".//a:t", namespaces=ns):
        if node.text:
            tokens.append(node.text)
    return " ".join(tokens)


def _classify_slide(slide_text: str) -> SlideKind:
    text = slide_text
    if "Algorithmic choice relocates power" in text:
        return "COVER"
    if "System framing:" in text:
        return "SYSTEM"
    if "Research questions" in text:
        return "RQS"
    if "Data artifacts collected" in text:
        return "ARTIFACTS"
    if "Collection credibility" in text:
        return "CREDIBILITY"
    if "What this enables next" in text:
        return "NEXT"
    if "Appendix:" in text:
        return "APPENDIX"
    if "Backup:" in text:
        if "API surfaces" in text:
            return "BACKUP_API"
        if "folder layout" in text:
            return "BACKUP_FOLDER"
        return "BACKUP"
    if "Bluesky UI:" in text:
        return "UI_SCREENSHOT"
    if "Data receipt:" in text:
        return "RECEIPT"
    return "UNKNOWN"


def _structured_steps_for_kind_v16(kind: SlideKind) -> list[list[int]] | None:
    # Each inner list is one click step; shapes not mentioned remain static/visible.
    match kind:
        case "COVER":
            return [
                [7, 8],  # title + subtitle
                [9, 10],  # thesis texture + card
                [11],  # thesis text
                [12],  # "NO RESULTS TODAY" badge
                [13],  # hero illustration
                [14],  # semantic icon
            ]
        case "SYSTEM":
            return [
                [7, 8],  # title + takeaway
                [9],
                [10],
                [11],
                [12],
                [13],
                [14],  # nodes (labelers last)
                [15, 16, 17, 18],  # horizontal connectors
                [19, 20],  # vertical connectors
                [21],  # bottom sentence
                [22, 23],  # illustration + icon
            ]
        case "UI_SCREENSHOT":
            return [
                [6, 7],  # title + takeaway
                [8],  # main screenshot
                [9],  # small icon
                [10],
                [11],
                [12],
                [13],
                [14],
                [15],
                [16],  # chips cascade
                [17, 18, 23],  # A badge + highlight + connector
                [19, 20, 24],  # B badge + highlight + connector
                [21, 22],  # C badge + highlight
            ]
        case "RQS":
            return [
                [7, 8],  # title + takeaway
                [9, 10],
                [11, 12],
                [13, 14],
                [15, 16],
                [17, 18],
                [19],  # donut accent
            ]
        case "ARTIFACTS":
            return [
                [8],  # title
                [9],  # discovery inputs card
                [10],  # panel snapshots card
                [11],  # trust/repro card
                [12],  # join spine card
                [13],  # illustration
            ]
        case "RECEIPT":
            return [
                [6, 7],  # title + takeaway
                [8, 9],  # receipt stack + small icon
                [10],
                [11],
                [12],
                [13],
                [14],
                [15],
                [16],  # chips cascade
                [17, 18],  # A badge + highlight rect
                [19, 20],  # B badge + highlight rect
                [21, 22],  # C badge + highlight rect
            ]
        case "CREDIBILITY":
            return [
                [8],  # title
                [9],  # left card
                [10],  # right card
            ]
        case "NEXT":
            return [
                [7],  # title
                [8],  # ready measurements card
                [9],  # advisor asks card
                [10],  # illustration
            ]
        case "BACKUP_API":
            return [
                [7],
                [8],
            ]
        case "BACKUP_FOLDER":
            return [
                [7],
                [8],
                [9, 10],
            ]
        case "BACKUP":
            return [
                [7],
                [8],
            ]
        case _:
            return None


def _structured_steps_for_kind_v17(kind: SlideKind) -> StructuredSteps | None:
    """Structured reveal grammar for the re-templated v17 deck (name-addressed shapes)."""

    match kind:
        case "COVER":
            return [
                ["TITLE"],
                ["SUBTITLE"],
                ["BADGE_NO_RESULTS"],
            ]
        case "SYSTEM":
            return [
                ["TITLE", "TAKEAWAY"],
                ["NODE_VIEWER"],
                ["NODE_DISCOVERY"],
                ["NODE_FEEDS"],
                ["NODE_HOSTING"],
                ["NODE_OUTCOMES"],
                ["NODE_LABELERS"],
                ["LINK_1", "LINK_2", "LINK_3", "LINK_4", "LINK_LABELERS"],
                ["BOTTOM"],
            ]
        case "UI_SCREENSHOT":
            return [
                ["TITLE", "TAKEAWAY"],
                ["DIVIDER", "IMAGE_FRAME", "MAIN_IMAGE"],
                ["CHIP_01"],
                ["CHIP_02"],
                ["CHIP_03"],
                ["CHIP_04"],
                ["CHIP_05"],
                ["CHIP_06"],
                ["CHIP_07"],
                ["BADGE_A", "HILITE_A", "CONN_A"],
                ["BADGE_B", "HILITE_B", "CONN_B"],
                ["BADGE_C", "HILITE_C"],
            ]
        case "RECEIPT":
            # Same grammar as UI screenshot slides; connector shapes may be absent.
            return _structured_steps_for_kind_v17("UI_SCREENSHOT")
        case "RQS":
            return [
                ["TITLE", "TAKEAWAY"],
                ["RQ_NUM_01", "RQ_SLOT_01"],
                ["RQ_NUM_02", "RQ_SLOT_02"],
                ["RQ_NUM_03", "RQ_SLOT_03"],
                ["RQ_NUM_04", "RQ_SLOT_04"],
                ["RQ_NUM_05", "RQ_SLOT_05"],
            ]
        case "ARTIFACTS":
            return [
                ["TITLE", "TAKEAWAY"],
                ["GROUP_01_NUM", "GROUP_01_HEAD", "GROUP_01_BODY"],
                ["GROUP_02_NUM", "GROUP_02_HEAD", "GROUP_02_BODY"],
                ["GROUP_03_NUM", "GROUP_03_HEAD", "GROUP_03_BODY"],
                ["GROUP_04_NUM", "GROUP_04_HEAD", "GROUP_04_BODY"],
            ]
        case "CREDIBILITY":
            return [
                ["TITLE"],
                ["CARD_LEFT"],
                ["CARD_RIGHT"],
            ]
        case "NEXT":
            return [
                ["TITLE"],
                ["CARD_LEFT"],
                ["CARD_RIGHT"],
            ]
        case "APPENDIX" | "BACKUP_API" | "BACKUP_FOLDER" | "BACKUP":
            return [
                ["TITLE"],
                ["BODY"],
            ]
        case _:
            return None


def _iter_spids_in_z_order(slide_root: etree._Element) -> list[int]:
    sp_tree = slide_root.find(f"{_qn(PML_NS, 'cSld')}/{_qn(PML_NS, 'spTree')}")
    if sp_tree is None:
        return []

    spids: list[int] = []
    for child in sp_tree:
        local = etree.QName(child).localname
        if local in {"nvGrpSpPr", "grpSpPr"}:
            continue
        c_nv_pr = child.find(f".//{_qn(PML_NS, 'cNvPr')}")
        if c_nv_pr is None:
            continue
        raw = c_nv_pr.get("id")
        if raw is None:
            continue
        try:
            spids.append(int(raw))
        except ValueError:
            continue
    return spids


@dataclass(frozen=True)
class ShapeInfo:
    spid: int
    name: str


def _iter_shape_infos_in_z_order(slide_root: etree._Element) -> list[ShapeInfo]:
    sp_tree = slide_root.find(f"{_qn(PML_NS, 'cSld')}/{_qn(PML_NS, 'spTree')}")
    if sp_tree is None:
        return []

    infos: list[ShapeInfo] = []
    for child in sp_tree:
        local = etree.QName(child).localname
        if local in {"nvGrpSpPr", "grpSpPr"}:
            continue
        c_nv_pr = child.find(f".//{_qn(PML_NS, 'cNvPr')}")
        if c_nv_pr is None:
            continue
        raw = c_nv_pr.get("id")
        if raw is None:
            continue
        try:
            spid = int(raw)
        except ValueError:
            continue
        infos.append(ShapeInfo(spid=spid, name=c_nv_pr.get("name") or ""))
    return infos


def _build_name_to_spid(infos: Sequence[ShapeInfo]) -> dict[str, int]:
    name_to_spid: dict[str, int] = {}
    for info in infos:
        if not info.name:
            continue
        # If duplicates exist, keep the last occurrence (top-most in z-order).
        name_to_spid[info.name] = info.spid
    return name_to_spid


def _ensure_fade_transition(root: etree._Element) -> None:
    transition = root.find(_qn(PML_NS, "transition"))
    if transition is None:
        transition = etree.Element(_qn(PML_NS, "transition"), spd="slow")
        etree.SubElement(transition, _qn(PML_NS, "fade"))
        clr_map = root.find(_qn(PML_NS, "clrMapOvr"))
        if clr_map is not None:
            idx = list(root).index(clr_map)
            root.insert(idx + 1, transition)
            return
        c_sld = root.find(_qn(PML_NS, "cSld"))
        if c_sld is not None:
            idx = list(root).index(c_sld)
            root.insert(idx + 1, transition)
            return
        root.insert(0, transition)
        return

    if transition.get("spd") is None:
        transition.set("spd", "slow")
    if transition.find(_qn(PML_NS, "fade")) is None:
        for child in list(transition):
            transition.remove(child)
        etree.SubElement(transition, _qn(PML_NS, "fade"))


def _filter_structured_steps(
    *,
    steps: StructuredSteps,
    spids_in_z: Sequence[int],
    name_to_spid: Mapping[str, int],
    exclude_spids: set[int],
) -> list[list[int]]:
    available = set(spids_in_z)
    filtered_steps: list[list[int]] = []
    used: set[int] = set()

    for step in steps:
        raw_step: set[int] = set()
        for target in step:
            spid: int | None
            if isinstance(target, int):
                spid = target
            else:
                spid = name_to_spid.get(target)
            if spid is None or spid in used or spid in exclude_spids or spid not in available:
                continue
            used.add(spid)
            raw_step.add(spid)

        if raw_step:
            filtered_steps.append([spid for spid in spids_in_z if spid in raw_step])

    return filtered_steps


def _build_click_timing(*, spids: Iterable[int], effect_dur_ms: int) -> etree._Element:
    timing = etree.Element(_qn(PML_NS, "timing"))

    tn_lst = etree.SubElement(timing, _qn(PML_NS, "tnLst"))
    par = etree.SubElement(tn_lst, _qn(PML_NS, "par"))

    tm_root = etree.SubElement(
        par,
        _qn(PML_NS, "cTn"),
        id="1",
        dur="indefinite",
        restart="never",
        nodeType="tmRoot",
    )
    tm_child = etree.SubElement(tm_root, _qn(PML_NS, "childTnLst"))

    seq = etree.SubElement(tm_child, _qn(PML_NS, "seq"), concurrent="1", nextAc="seek")
    main_ctn = etree.SubElement(seq, _qn(PML_NS, "cTn"), id="2", dur="indefinite", nodeType="mainSeq")
    main_child = etree.SubElement(main_ctn, _qn(PML_NS, "childTnLst"))

    next_id = 3
    for spid in spids:
        # Wrapper: keep in sync with patterns seen in Slide2_RQs_and_Data.pptx (clickEffect + fade).
        par_wrap = etree.SubElement(main_child, _qn(PML_NS, "par"))
        wrap = etree.SubElement(par_wrap, _qn(PML_NS, "cTn"), id=str(next_id), fill="hold")
        next_id += 1

        st_cond = etree.SubElement(wrap, _qn(PML_NS, "stCondLst"))
        etree.SubElement(st_cond, _qn(PML_NS, "cond"), delay="indefinite")
        cond_on_begin = etree.SubElement(st_cond, _qn(PML_NS, "cond"), evt="onBegin", delay="0")
        etree.SubElement(cond_on_begin, _qn(PML_NS, "tn"), val="2")

        wrap_child = etree.SubElement(wrap, _qn(PML_NS, "childTnLst"))
        par_hold = etree.SubElement(wrap_child, _qn(PML_NS, "par"))
        hold = etree.SubElement(par_hold, _qn(PML_NS, "cTn"), id=str(next_id), fill="hold")
        next_id += 1

        hold_cond = etree.SubElement(hold, _qn(PML_NS, "stCondLst"))
        etree.SubElement(hold_cond, _qn(PML_NS, "cond"), delay="0")

        hold_child = etree.SubElement(hold, _qn(PML_NS, "childTnLst"))
        par_effect = etree.SubElement(hold_child, _qn(PML_NS, "par"))
        click = etree.SubElement(
            par_effect,
            _qn(PML_NS, "cTn"),
            id=str(next_id),
            presetID="10",
            presetClass="entr",
            presetSubtype="0",
            fill="hold",
            grpId="0",
            nodeType="clickEffect",
        )
        next_id += 1

        click_cond = etree.SubElement(click, _qn(PML_NS, "stCondLst"))
        etree.SubElement(click_cond, _qn(PML_NS, "cond"), delay="0")

        click_child = etree.SubElement(click, _qn(PML_NS, "childTnLst"))

        # First: set visibility=visible
        set_el = etree.SubElement(click_child, _qn(PML_NS, "set"))
        c_bhvr = etree.SubElement(set_el, _qn(PML_NS, "cBhvr"))
        set_ctn = etree.SubElement(c_bhvr, _qn(PML_NS, "cTn"), id=str(next_id), dur="1", fill="hold")
        next_id += 1
        set_ctn_cond = etree.SubElement(set_ctn, _qn(PML_NS, "stCondLst"))
        etree.SubElement(set_ctn_cond, _qn(PML_NS, "cond"), delay="0")

        tgt_el = etree.SubElement(c_bhvr, _qn(PML_NS, "tgtEl"))
        etree.SubElement(tgt_el, _qn(PML_NS, "spTgt"), spid=str(spid))
        attr_name_lst = etree.SubElement(c_bhvr, _qn(PML_NS, "attrNameLst"))
        attr_name = etree.SubElement(attr_name_lst, _qn(PML_NS, "attrName"))
        attr_name.text = "style.visibility"

        to = etree.SubElement(set_el, _qn(PML_NS, "to"))
        etree.SubElement(to, _qn(PML_NS, "strVal"), val="visible")

        # Second: entrance effect (fade)
        anim = etree.SubElement(click_child, _qn(PML_NS, "animEffect"), transition="in", filter="fade")
        anim_bhvr = etree.SubElement(anim, _qn(PML_NS, "cBhvr"))
        etree.SubElement(anim_bhvr, _qn(PML_NS, "cTn"), id=str(next_id), dur=str(int(effect_dur_ms)))
        next_id += 1
        anim_tgt = etree.SubElement(anim_bhvr, _qn(PML_NS, "tgtEl"))
        etree.SubElement(anim_tgt, _qn(PML_NS, "spTgt"), spid=str(spid))

    prev = etree.SubElement(seq, _qn(PML_NS, "prevCondLst"))
    cond_prev = etree.SubElement(prev, _qn(PML_NS, "cond"), evt="onPrev", delay="0")
    tgt_prev = etree.SubElement(cond_prev, _qn(PML_NS, "tgtEl"))
    etree.SubElement(tgt_prev, _qn(PML_NS, "sldTgt"))

    nxt = etree.SubElement(seq, _qn(PML_NS, "nextCondLst"))
    cond_next = etree.SubElement(nxt, _qn(PML_NS, "cond"), evt="onNext", delay="0")
    tgt_next = etree.SubElement(cond_next, _qn(PML_NS, "tgtEl"))
    etree.SubElement(tgt_next, _qn(PML_NS, "sldTgt"))

    bld_lst = etree.SubElement(timing, _qn(PML_NS, "bldLst"))
    for spid in spids:
        etree.SubElement(bld_lst, _qn(PML_NS, "bldP"), spid=str(spid), grpId="0", animBg="1")

    return timing


def _build_click_timing_steps(*, steps: list[list[int]], effect_dur_ms: int) -> etree._Element:
    timing = etree.Element(_qn(PML_NS, "timing"))

    tn_lst = etree.SubElement(timing, _qn(PML_NS, "tnLst"))
    par = etree.SubElement(tn_lst, _qn(PML_NS, "par"))

    tm_root = etree.SubElement(
        par,
        _qn(PML_NS, "cTn"),
        id="1",
        dur="indefinite",
        restart="never",
        nodeType="tmRoot",
    )
    tm_child = etree.SubElement(tm_root, _qn(PML_NS, "childTnLst"))

    seq = etree.SubElement(tm_child, _qn(PML_NS, "seq"), concurrent="1", nextAc="seek")
    main_ctn = etree.SubElement(seq, _qn(PML_NS, "cTn"), id="2", dur="indefinite", nodeType="mainSeq")
    main_child = etree.SubElement(main_ctn, _qn(PML_NS, "childTnLst"))

    animated_spids: list[int] = []
    next_id = 3
    for step in steps:
        if not step:
            continue
        animated_spids.extend(step)

        par_wrap = etree.SubElement(main_child, _qn(PML_NS, "par"))
        wrap = etree.SubElement(par_wrap, _qn(PML_NS, "cTn"), id=str(next_id), fill="hold")
        next_id += 1

        st_cond = etree.SubElement(wrap, _qn(PML_NS, "stCondLst"))
        etree.SubElement(st_cond, _qn(PML_NS, "cond"), delay="indefinite")
        cond_on_begin = etree.SubElement(st_cond, _qn(PML_NS, "cond"), evt="onBegin", delay="0")
        etree.SubElement(cond_on_begin, _qn(PML_NS, "tn"), val="2")

        wrap_child = etree.SubElement(wrap, _qn(PML_NS, "childTnLst"))
        par_hold = etree.SubElement(wrap_child, _qn(PML_NS, "par"))
        hold = etree.SubElement(par_hold, _qn(PML_NS, "cTn"), id=str(next_id), fill="hold")
        next_id += 1

        hold_cond = etree.SubElement(hold, _qn(PML_NS, "stCondLst"))
        etree.SubElement(hold_cond, _qn(PML_NS, "cond"), delay="0")

        hold_child = etree.SubElement(hold, _qn(PML_NS, "childTnLst"))
        par_effect = etree.SubElement(hold_child, _qn(PML_NS, "par"))
        click = etree.SubElement(
            par_effect,
            _qn(PML_NS, "cTn"),
            id=str(next_id),
            presetID="10",
            presetClass="entr",
            presetSubtype="0",
            fill="hold",
            grpId="0",
            nodeType="clickEffect",
        )
        next_id += 1

        click_cond = etree.SubElement(click, _qn(PML_NS, "stCondLst"))
        etree.SubElement(click_cond, _qn(PML_NS, "cond"), delay="0")

        click_child = etree.SubElement(click, _qn(PML_NS, "childTnLst"))
        for spid in step:
            # Set visibility=visible
            set_el = etree.SubElement(click_child, _qn(PML_NS, "set"))
            c_bhvr = etree.SubElement(set_el, _qn(PML_NS, "cBhvr"))
            set_ctn = etree.SubElement(c_bhvr, _qn(PML_NS, "cTn"), id=str(next_id), dur="1", fill="hold")
            next_id += 1
            set_ctn_cond = etree.SubElement(set_ctn, _qn(PML_NS, "stCondLst"))
            etree.SubElement(set_ctn_cond, _qn(PML_NS, "cond"), delay="0")

            tgt_el = etree.SubElement(c_bhvr, _qn(PML_NS, "tgtEl"))
            etree.SubElement(tgt_el, _qn(PML_NS, "spTgt"), spid=str(spid))
            attr_name_lst = etree.SubElement(c_bhvr, _qn(PML_NS, "attrNameLst"))
            attr_name = etree.SubElement(attr_name_lst, _qn(PML_NS, "attrName"))
            attr_name.text = "style.visibility"

            to = etree.SubElement(set_el, _qn(PML_NS, "to"))
            etree.SubElement(to, _qn(PML_NS, "strVal"), val="visible")

            # Entrance effect (fade)
            anim = etree.SubElement(click_child, _qn(PML_NS, "animEffect"), transition="in", filter="fade")
            anim_bhvr = etree.SubElement(anim, _qn(PML_NS, "cBhvr"))
            etree.SubElement(anim_bhvr, _qn(PML_NS, "cTn"), id=str(next_id), dur=str(int(effect_dur_ms)))
            next_id += 1
            anim_tgt = etree.SubElement(anim_bhvr, _qn(PML_NS, "tgtEl"))
            etree.SubElement(anim_tgt, _qn(PML_NS, "spTgt"), spid=str(spid))

    prev = etree.SubElement(seq, _qn(PML_NS, "prevCondLst"))
    cond_prev = etree.SubElement(prev, _qn(PML_NS, "cond"), evt="onPrev", delay="0")
    tgt_prev = etree.SubElement(cond_prev, _qn(PML_NS, "tgtEl"))
    etree.SubElement(tgt_prev, _qn(PML_NS, "sldTgt"))

    nxt = etree.SubElement(seq, _qn(PML_NS, "nextCondLst"))
    cond_next = etree.SubElement(nxt, _qn(PML_NS, "cond"), evt="onNext", delay="0")
    tgt_next = etree.SubElement(cond_next, _qn(PML_NS, "tgtEl"))
    etree.SubElement(tgt_next, _qn(PML_NS, "sldTgt"))

    bld_lst = etree.SubElement(timing, _qn(PML_NS, "bldLst"))
    seen: set[int] = set()
    for spid in animated_spids:
        if spid in seen:
            continue
        seen.add(spid)
        etree.SubElement(bld_lst, _qn(PML_NS, "bldP"), spid=str(spid), grpId="0", animBg="1")

    return timing


def _replace_slide_timing(
    *, slide_xml: bytes, exclude_spids: set[int], effect_dur_ms: int
) -> tuple[bytes, int]:
    root = etree.fromstring(slide_xml)

    spids = [spid for spid in _iter_spids_in_z_order(root) if spid not in exclude_spids]
    click_effects = len(spids)

    # Remove any existing timing blocks.
    for existing in list(root.findall(_qn(PML_NS, "timing"))):
        root.remove(existing)

    timing = _build_click_timing(spids=spids, effect_dur_ms=effect_dur_ms)

    _ensure_fade_transition(root)
    transition = root.find(_qn(PML_NS, "transition"))
    if transition is not None:
        idx = list(root).index(transition)
        root.insert(idx + 1, timing)
    else:
        root.append(timing)

    out_xml = etree.tostring(root, encoding="UTF-8", xml_declaration=False)
    return out_xml, click_effects


def _replace_slide_timing_structured(
    *,
    slide_xml: bytes,
    exclude_spids: set[int],
    effect_dur_ms: int,
) -> tuple[bytes, int]:
    root = etree.fromstring(slide_xml)
    slide_text = _extract_slide_text(root)
    kind = _classify_slide(slide_text)

    spids_in_z = _iter_spids_in_z_order(root)
    name_to_spid = _build_name_to_spid(_iter_shape_infos_in_z_order(root))

    # Prefer name-addressed steps (v17); fall back to the older numeric tables.
    steps_v17 = _structured_steps_for_kind_v17(kind)
    filtered_steps: list[list[int]] = []
    if steps_v17 is not None:
        filtered_steps = _filter_structured_steps(
            steps=steps_v17,
            spids_in_z=spids_in_z,
            name_to_spid=name_to_spid,
            exclude_spids=exclude_spids,
        )

    if not filtered_steps:
        steps_v16 = _structured_steps_for_kind_v16(kind)
        if steps_v16 is None:
            patched, clicks = _replace_slide_timing(slide_xml=slide_xml, exclude_spids=exclude_spids, effect_dur_ms=effect_dur_ms)
            return patched, clicks
        filtered_steps = _filter_structured_steps(
            steps=steps_v16,
            spids_in_z=spids_in_z,
            name_to_spid=name_to_spid,
            exclude_spids=exclude_spids,
        )

    click_effects = len(filtered_steps)

    for existing in list(root.findall(_qn(PML_NS, "timing"))):
        root.remove(existing)

    timing = _build_click_timing_steps(steps=filtered_steps, effect_dur_ms=effect_dur_ms)

    _ensure_fade_transition(root)
    transition = root.find(_qn(PML_NS, "transition"))
    if transition is not None:
        idx = list(root).index(transition)
        root.insert(idx + 1, timing)
    else:
        root.append(timing)

    out_xml = etree.tostring(root, encoding="UTF-8", xml_declaration=False)
    return out_xml, click_effects


def inject_click_reveals(
    *,
    pptx_in: Path,
    pptx_out: Path,
    slide_nums: set[int] | None = None,
    exclude_spids: set[int] | None = None,
    effect_dur_ms: int = 240,
) -> InjectResult:
    """Inject click-to-reveal entrance animations for most shapes on each slide.

    Notes:
    - `python-pptx` cannot author animations; we patch slide XML directly.
    - We follow an OOXML pattern already present in `Slide2_RQs_and_Data.pptx`.
    """

    if exclude_spids is None:
        # Default: keep background rectangle and overlay image visible.
        exclude_spids = {2, 3}

    pptx_in = pptx_in.resolve()
    pptx_out = pptx_out.resolve()
    pptx_out.parent.mkdir(parents=True, exist_ok=True)

    slide_click_effects: dict[int, int] = {}

    with zipfile.ZipFile(pptx_in, "r") as zin:
        with zipfile.ZipFile(pptx_out, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                match = _SLIDE_XML_RE.match(info.filename)
                if match:
                    slide_num = int(match.group(1))
                    if slide_nums is not None and slide_num not in slide_nums:
                        zout.writestr(info, data)
                        continue
                    patched, clicks = _replace_slide_timing(
                        slide_xml=data,
                        exclude_spids=set(exclude_spids),
                        effect_dur_ms=effect_dur_ms,
                    )
                    slide_click_effects[slide_num] = clicks
                    zout.writestr(info, patched)
                    continue

                zout.writestr(info, data)

    return InjectResult(pptx_in=pptx_in, pptx_out=pptx_out, slide_click_effects=slide_click_effects)


def inject_structured_reveals(
    *,
    pptx_in: Path,
    pptx_out: Path,
    slide_nums: set[int] | None = None,
    exclude_spids: set[int] | None = None,
    effect_dur_ms: int = 220,
) -> InjectResult:
    """Inject click-to-reveal animations using a structured per-slide reveal grammar.

    Falls back to the legacy per-shape reveal injector for unknown slide types.
    """

    if exclude_spids is None:
        exclude_spids = {2, 3}

    pptx_in = pptx_in.resolve()
    pptx_out = pptx_out.resolve()
    pptx_out.parent.mkdir(parents=True, exist_ok=True)

    slide_click_effects: dict[int, int] = {}

    with zipfile.ZipFile(pptx_in, "r") as zin:
        with zipfile.ZipFile(pptx_out, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                match = _SLIDE_XML_RE.match(info.filename)
                if match:
                    slide_num = int(match.group(1))
                    if slide_nums is not None and slide_num not in slide_nums:
                        zout.writestr(info, data)
                        continue
                    patched, clicks = _replace_slide_timing_structured(
                        slide_xml=data,
                        exclude_spids=set(exclude_spids),
                        effect_dur_ms=effect_dur_ms,
                    )
                    slide_click_effects[slide_num] = clicks
                    zout.writestr(info, patched)
                    continue

                zout.writestr(info, data)

    return InjectResult(pptx_in=pptx_in, pptx_out=pptx_out, slide_click_effects=slide_click_effects)


def main() -> None:
    import argparse
    import sys
    import tempfile

    parser = argparse.ArgumentParser(description="Inject click-reveal animations into a PPTX (OOXML patch).")
    parser.add_argument("pptx", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--effect-dur-ms", type=int, default=240)
    parser.add_argument("--exclude-spids", type=str, default="2,3", help="Comma-separated shape IDs to not animate.")
    parser.add_argument("--legacy", action="store_true", help="Use legacy: animate most shapes one-by-one.")
    args = parser.parse_args()

    in_path = args.pptx.resolve()
    if not in_path.exists():
        raise SystemExit(f"Missing pptx: {in_path}")

    out_path = args.out.resolve() if args.out is not None else in_path
    exclude: set[int] = set()
    for token in args.exclude_spids.split(","):
        token = token.strip()
        if token:
            exclude.add(int(token))

    if out_path == in_path:
        with tempfile.TemporaryDirectory(prefix="pptx_anim_") as tmp_dir:
            tmp_out = Path(tmp_dir) / in_path.name
            if args.legacy:
                res = inject_click_reveals(
                    pptx_in=in_path,
                    pptx_out=tmp_out,
                    exclude_spids=exclude,
                    effect_dur_ms=int(args.effect_dur_ms),
                )
            else:
                res = inject_structured_reveals(
                    pptx_in=in_path,
                    pptx_out=tmp_out,
                    exclude_spids=exclude,
                    effect_dur_ms=int(args.effect_dur_ms),
                )
            out_path.write_bytes(tmp_out.read_bytes())
    else:
        if args.legacy:
            res = inject_click_reveals(
                pptx_in=in_path,
                pptx_out=out_path,
                exclude_spids=exclude,
                effect_dur_ms=int(args.effect_dur_ms),
            )
        else:
            res = inject_structured_reveals(
                pptx_in=in_path,
                pptx_out=out_path,
                exclude_spids=exclude,
                effect_dur_ms=int(args.effect_dur_ms),
            )

    print(f"OK: wrote {res.pptx_out}")
    print(f"OK: clickEffects total={res.total_click_effects}")


if __name__ == "__main__":
    main()
