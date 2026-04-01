#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import build_ifx_poster_svg as base


PALETTE: dict[str, str] = {
    "bg": "08121E",
    "ink": "F4F8FD",
    "muted": "C7D6E8",
    "card": "12233A",
    "card2": "0C1A2B",
    "card_line": "32567A",
    "cyan": "67C7FF",
    "amber": "E3BC69",
    "coral": "C58A53",
    "green": "67D49A",
    "violet": "958AFF",
}


def _hex_rgb(hex6: str) -> tuple[int, int, int]:
    return int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16)


def _mix(a: str, b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    ar, ag, ab = _hex_rgb(a)
    br, bg, bb = _hex_rgb(b)
    rr = int(round(ar + (br - ar) * t))
    rg = int(round(ag + (bg - ag) * t))
    rb = int(round(ab + (bb - ab) * t))
    return f"{rr:02X}{rg:02X}{rb:02X}"


def _shadow_rect(*, x: int, y: int, w: int, h: int, r: int, fill: str, opacity: float, dx: int = 0, dy: int = 8) -> str:
    return (
        f'<rect x="{x+dx}" y="{y+dy}" width="{w}" height="{h}" rx="{r}" ry="{r}" '
        f'fill="{fill}" opacity="{opacity:.3f}"/>'
    )


def _root_with_bg(*, w_px: int, h_px: int, defs: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w_px}" height="{h_px}" '
        f'viewBox="0 0 {w_px} {h_px}" version="1.1" overflow="hidden"><defs>{defs}</defs>{body}</svg>'
    )


def _patch_theme() -> None:
    base.COLORS_HEX.update(PALETTE)
    base.FONT_SANS = "Trebuchet MS"

    def _svg_text(
        *,
        x: int,
        y: int,
        text: str,
        size_px: int,
        color: str,
        weight: int = 400,
        anchor: str = "start",
        family: str | None = None,
        opacity: float = 1.0,
        baseline: str = "middle",
    ) -> str:
        if family is None:
            family = base.FONT_SANS
        op = f' opacity="{opacity:.4f}"' if opacity < 1.0 else ""
        return (
            f'<text x="{x}" y="{y}" fill="{color}" font-family="{base._escape_xml(family)}" '
            f'font-size="{size_px}" font-weight="{weight}" text-anchor="{anchor}" '
            f'dominant-baseline="{baseline}" letter-spacing="0.1px"{op}>{base._escape_xml(text)}</text>'
        )

    def _svg_multiline(
        *,
        x: int,
        y: int,
        lines: list[str],
        size_px: int,
        color: str,
        weight: int = 400,
        anchor: str = "start",
        family: str | None = None,
        line_gap_px: int | None = None,
        baseline: str = "hanging",
    ) -> str:
        if family is None:
            family = base.FONT_SANS
        if line_gap_px is None:
            line_gap_px = int(round(size_px * 1.25))
        tspans = []
        for i, line in enumerate(lines):
            dy = 0 if i == 0 else line_gap_px
            tspans.append(f'<tspan x="{x}" dy="{dy}">{base._escape_xml(line)}</tspan>')
        return (
            f'<text x="{x}" y="{y}" fill="{color}" font-family="{base._escape_xml(family)}" '
            f'font-size="{size_px}" font-weight="{weight}" text-anchor="{anchor}" '
            f'dominant-baseline="{baseline}" letter-spacing="0.1px">{"".join(tspans)}</text>'
        )

    def _svg_grid_bg(*, w_px: int, h_px: int) -> str:
        step = 46
        defs = (
            '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0%" stop-color="#{PALETTE["bg"]}"/>'
            f'<stop offset="100%" stop-color="#06101A"/>'
            "</linearGradient>"
        )
        body = f'<rect x="0" y="0" width="{w_px}" height="{h_px}" fill="url(#bg)"/>'
        body += f'<circle cx="{int(w_px*0.18)}" cy="{int(h_px*0.15)}" r="{int(min(w_px,h_px)*0.22)}" fill="#{PALETTE["cyan"]}" opacity="0.055"/>'
        body += f'<circle cx="{int(w_px*0.84)}" cy="{int(h_px*0.76)}" r="{int(min(w_px,h_px)*0.27)}" fill="#{PALETTE["coral"]}" opacity="0.045"/>'
        for x in range(0, w_px + 1, step):
            alpha = 0.05 if x % (step * 4) == 0 else 0.028
            body += f'<line x1="{x}" y1="0" x2="{x}" y2="{h_px}" stroke="#FFFFFF" stroke-opacity="{alpha:.3f}" stroke-width="1"/>'
        for y in range(0, h_px + 1, step):
            alpha = 0.05 if y % (step * 4) == 0 else 0.028
            body += f'<line x1="0" y1="{y}" x2="{w_px}" y2="{y}" stroke="#FFFFFF" stroke-opacity="{alpha:.3f}" stroke-width="1"/>'
        body += f'<rect x="0" y="0" width="{w_px}" height="{h_px}" fill="#000000" opacity="0.08"/>'
        return _root_with_bg(w_px=w_px, h_px=h_px, defs=defs, body=body)

    def _svg_title_block(*, w_px: int, h_px: int, title: str, subtitle: str) -> str:
        pad = 22
        defs = (
            '<linearGradient id="titleGrad" x1="0" y1="0" x2="1" y2="0">'
            f'<stop offset="0%" stop-color="#{_mix(PALETTE["card"], "1B3354", 0.30)}"/>'
            f'<stop offset="100%" stop-color="#{PALETTE["card"]}"/>'
            "</linearGradient>"
        )
        body = _shadow_rect(x=0, y=0, w=w_px, h=h_px, r=20, fill="#000000", opacity=0.22, dy=10)
        body += f'<rect x="0" y="0" width="{w_px}" height="{h_px}" rx="20" ry="20" fill="url(#titleGrad)" stroke="#{PALETTE["card_line"]}" stroke-width="2"/>'
        body += f'<rect x="0" y="0" width="14" height="{h_px}" rx="20" ry="20" fill="#{PALETTE["cyan"]}" opacity="0.92"/>'
        body += f'<rect x="0" y="{int(round(h_px*0.54))}" width="14" height="{h_px - int(round(h_px*0.54))}" rx="20" ry="20" fill="#{PALETTE["coral"]}" opacity="0.88"/>'
        body += f'<line x1="16" y1="2" x2="{w_px-18}" y2="2" stroke="#FFFFFF" stroke-opacity="0.14" stroke-width="2"/>'
        body += f'<line x1="16" y1="{h_px-2}" x2="{w_px-18}" y2="{h_px-2}" stroke="#000000" stroke-opacity="0.18" stroke-width="2"/>'
        body += _svg_text(x=pad + 2, y=int(h_px * 0.38), text=title, size_px=44, color=f'#{PALETTE["ink"]}', weight=900)
        body += _svg_text(x=pad + 2, y=int(h_px * 0.77), text=subtitle, size_px=18, color=f'#{PALETTE["muted"]}', weight=600)
        return _root_with_bg(w_px=w_px, h_px=h_px, defs=defs, body=body)

    def _svg_panel_label(*, w_px: int, h_px: int, text: str, accent_hex: str) -> str:
        defs = (
            '<linearGradient id="panelGrad" x1="0" y1="0" x2="1" y2="0">'
            f'<stop offset="0%" stop-color="#{_mix(PALETTE["card2"], accent_hex, 0.10)}"/>'
            f'<stop offset="100%" stop-color="#{PALETTE["card2"]}"/>'
            "</linearGradient>"
        )
        body = _shadow_rect(x=0, y=0, w=w_px, h=h_px, r=15, fill="#000000", opacity=0.18, dy=5)
        body += f'<rect x="0" y="0" width="{w_px}" height="{h_px}" rx="15" ry="15" fill="url(#panelGrad)" stroke="#{accent_hex}" stroke-width="2"/>'
        body += f'<circle cx="18" cy="{h_px//2}" r="{max(4, int(round(h_px*0.18)))}" fill="#{accent_hex}" opacity="0.96"/>'
        body += f'<rect x="0" y="{h_px-4}" width="{max(50, int(round(w_px*0.55)))}" height="4" rx="4" ry="4" fill="#{accent_hex}" opacity="0.55"/>'
        body += _svg_text(x=34, y=h_px // 2, text=text, size_px=16, color=f'#{PALETTE["ink"]}', weight=800)
        return _root_with_bg(w_px=w_px, h_px=h_px, defs=defs, body=body)

    def _svg_pill(*, w_px: int, h_px: int, text: str, accent_hex: str) -> str:
        r = max(8, int(round(h_px / 2)))
        pad = int(round(h_px * 0.36))
        defs = (
            '<linearGradient id="pillGrad" x1="0" y1="0" x2="1" y2="0">'
            f'<stop offset="0%" stop-color="#{_mix(PALETTE["card2"], accent_hex, 0.12)}"/>'
            f'<stop offset="100%" stop-color="#{PALETTE["card2"]}"/>'
            "</linearGradient>"
        )
        body = f'<rect x="0" y="3" width="{w_px}" height="{h_px}" rx="{r}" ry="{r}" fill="#000000" opacity="0.16"/>'
        body += f'<rect x="0" y="0" width="{w_px}" height="{h_px}" rx="{r}" ry="{r}" fill="url(#pillGrad)" stroke="#{accent_hex}" stroke-width="2"/>'
        body += f'<circle cx="{pad}" cy="{h_px//2}" r="{max(4, int(round(h_px*0.18)))}" fill="#{accent_hex}" opacity="0.98"/>'
        body += f'<rect x="{max(10, pad+8)}" y="{max(3, int(round(h_px*0.17)))}" width="{max(28, int(round(w_px*0.25)))}" height="2" fill="#FFFFFF" opacity="0.18"/>'
        body += _svg_text(x=pad + int(round(h_px * 0.42)), y=h_px // 2, text=text, size_px=max(12, int(round(h_px * 0.55))), color=f'#{PALETTE["ink"]}', weight=850)
        return _root_with_bg(w_px=w_px, h_px=h_px, defs=defs, body=body)

    def _svg_chip(*, w_px: int, h_px: int, head: str, val: str, accent_hex: str) -> str:
        body = _shadow_rect(x=0, y=0, w=w_px, h=h_px, r=16, fill="#000000", opacity=0.17, dy=6)
        body += f'<rect x="0" y="0" width="{w_px}" height="{h_px}" rx="16" ry="16" fill="#{PALETTE["card2"]}" stroke="#{PALETTE["card_line"]}" stroke-width="2"/>'
        body += f'<rect x="0" y="0" width="10" height="{h_px}" rx="16" ry="16" fill="#{accent_hex}" opacity="0.55"/>'
        body += f'<rect x="16" y="12" width="{max(30, int(round(w_px*0.18)))}" height="3" rx="2" ry="2" fill="#{accent_hex}" opacity="0.40"/>'
        body += _svg_text(x=20, y=int(h_px * 0.35), text=head, size_px=14, color=f'#{PALETTE["muted"]}', weight=750)
        body += _svg_text(x=20, y=int(h_px * 0.74), text=val, size_px=17, color=f'#{accent_hex}', weight=900)
        return base._svg_root(w_px=w_px, h_px=h_px, body=body)

    def _svg_rank_row(*, w_px: int, h_px: int, rank: int, label: str, accent_hex: str) -> str:
        badge_w = max(int(round(w_px * 0.16)), int(round(h_px * 1.6)))
        av_r = max(4, int(round(h_px * 0.28)))
        av_cx = badge_w + int(round(av_r * 1.6))
        tx = av_cx + av_r + max(8, int(round(h_px * 0.22)))
        label_size = max(10, int(round(h_px * 0.56)))
        pad = 10
        bar_area_w = max(36, int(round(w_px * 0.22)))
        bar_h = max(4, int(round(h_px * 0.42)))
        bar_x = w_px - pad - bar_area_w
        bar_y = int(round((h_px - bar_h) / 2))
        frac = 1.0 / (1.0 + 0.18 * (max(1, rank) - 1))
        body = _shadow_rect(x=0, y=0, w=w_px, h=h_px, r=max(8, int(round(h_px / 2))), fill="#000000", opacity=0.18, dy=5)
        body += f'<rect x="0" y="0" width="{w_px}" height="{h_px}" rx="{max(8, int(round(h_px / 2)))}" ry="{max(8, int(round(h_px / 2)))}" fill="#{PALETTE["card2"]}" stroke="#{PALETTE["card_line"]}" stroke-width="2"/>'
        body += f'<rect x="0" y="0" width="{badge_w}" height="{h_px}" rx="{max(8, int(round(h_px / 2)))}" ry="{max(8, int(round(h_px / 2)))}" fill="#{accent_hex}"/>'
        body += f'<rect x="{badge_w-8}" y="0" width="8" height="{h_px}" fill="#FFFFFF" opacity="0.10"/>'
        body += _svg_text(x=badge_w // 2, y=h_px // 2, text=str(rank), size_px=max(11, int(round(h_px * 0.66))), color=f'#{PALETTE["bg"]}', weight=900, anchor="middle")
        body += f'<circle cx="{av_cx}" cy="{h_px//2}" r="{av_r}" fill="#{PALETTE["card_line"]}" opacity="0.95"/>'
        body += f'<circle cx="{av_cx-1}" cy="{h_px//2-1}" r="{max(2, av_r-2)}" fill="#{PALETTE["card"]}" opacity="0.88"/>'
        body += _svg_text(x=tx, y=h_px // 2, text=label, size_px=label_size, color=f'#{PALETTE["ink"]}', weight=700)
        body += f'<rect x="{bar_x}" y="{bar_y}" width="{bar_area_w}" height="{bar_h}" rx="8" ry="8" fill="#08111D" opacity="0.95"/>'
        body += f'<rect x="{bar_x}" y="{bar_y}" width="{int(round(bar_area_w * frac))}" height="{bar_h}" rx="8" ry="8" fill="#{accent_hex}" opacity="0.90"/>'
        body += f'<circle cx="{bar_x + int(round(bar_area_w * frac))}" cy="{bar_y + bar_h//2}" r="{max(3, bar_h//2 + 1)}" fill="#{accent_hex}" opacity="0.88"/>'
        return base._svg_root(w_px=w_px, h_px=h_px, body=body)

    def _svg_metric_card_bg(*, w_px: int, h_px: int, stroke_hex: str, kind: str) -> str:
        body = _shadow_rect(x=0, y=0, w=w_px, h=h_px, r=18, fill="#000000", opacity=0.19, dy=8)
        body += f'<rect x="0" y="0" width="{w_px}" height="{h_px}" rx="18" ry="18" fill="#{PALETTE["card2"]}" stroke="#{stroke_hex}" stroke-width="2"/>'
        body += f'<rect x="0" y="0" width="12" height="{h_px}" rx="18" ry="18" fill="#{stroke_hex}" opacity="0.18"/>'
        body += f'<line x1="12" y1="1" x2="{w_px-8}" y2="1" stroke="#FFFFFF" stroke-opacity="0.10" stroke-width="2"/>'
        if kind == "D":
            body += f'<rect x="{int(w_px*0.15)}" y="{int(h_px*0.78)}" width="{int(w_px*0.26)}" height="6" rx="4" ry="4" fill="#{stroke_hex}" opacity="0.6"/>'
        elif kind == "IF":
            body += f'<circle cx="{int(w_px*0.62)}" cy="{int(h_px*0.70)}" r="16" fill="#{stroke_hex}" opacity="0.18"/>'
            body += f'<circle cx="{int(w_px*0.78)}" cy="{int(h_px*0.70)}" r="16" fill="#FFFFFF" opacity="0.10"/>'
        elif kind == "DELTA":
            body += f'<rect x="{int(w_px*0.62)}" y="{int(h_px*0.70)}" width="{int(w_px*0.18)}" height="16" rx="8" ry="8" fill="#{stroke_hex}" opacity="0.18"/>'
        return base._svg_root(w_px=w_px, h_px=h_px, body=body)

    def _svg_bar_row(*, w_px: int, h_px: int, j: int, frac: float, accent_hex: str) -> str:
        r = max(8, int(round(h_px / 2)))
        pad = 10
        lab_w = max(20, int(round(w_px * 0.13)))
        track_x = pad
        track_w = w_px - pad * 2 - lab_w
        track_h = max(4, int(round(h_px * 0.46)))
        track_y = int(round((h_px - track_h) / 2))
        fill_w = int(round(track_w * frac))
        body = f'<rect x="0" y="3" width="{w_px}" height="{h_px}" rx="{r}" ry="{r}" fill="#000000" opacity="0.16"/>'
        body += f'<rect x="0" y="0" width="{w_px}" height="{h_px}" rx="{r}" ry="{r}" fill="#{PALETTE["card2"]}" stroke="#{PALETTE["card_line"]}" stroke-width="2"/>'
        body += f'<rect x="{track_x}" y="{track_y}" width="{track_w}" height="{track_h}" rx="8" ry="8" fill="#08111D" opacity="0.95"/>'
        body += f'<rect x="{track_x}" y="{track_y}" width="{fill_w}" height="{track_h}" rx="8" ry="8" fill="#{accent_hex}" opacity="0.92"/>'
        body += f'<circle cx="{track_x + fill_w}" cy="{track_y + track_h//2}" r="{max(3, track_h//2 + 1)}" fill="#{accent_hex}" opacity="0.88"/>'
        body += _svg_text(x=w_px - pad, y=h_px // 2, text=f"v{j}", size_px=max(10, int(round(h_px * 0.55))), color=f'#{PALETTE["muted"]}', weight=800, anchor="end")
        return base._svg_root(w_px=w_px, h_px=h_px, body=body)

    def _svg_scalar_bar(*, w_px: int, h_px: int, label: str, frac: float, accent_hex: str, value: str | None = None) -> str:
        pad = 14
        bar_h = max(8, int(round(h_px * 0.28)))
        bar_y = int(round(h_px * 0.62))
        body = _shadow_rect(x=0, y=0, w=w_px, h=h_px, r=16, fill="#000000", opacity=0.18, dy=6)
        body += f'<rect x="0" y="0" width="{w_px}" height="{h_px}" rx="16" ry="16" fill="#{PALETTE["card2"]}" stroke="#{PALETTE["card_line"]}" stroke-width="2"/>'
        body += _svg_text(x=pad, y=int(h_px * 0.28), text=label, size_px=max(12, int(round(h_px * 0.34))), color=f'#{PALETTE["muted"]}', weight=800, baseline="middle")
        body += f'<rect x="{pad}" y="{bar_y}" width="{w_px - 2*pad}" height="{bar_h}" rx="10" ry="10" fill="#08111D" opacity="0.96"/>'
        fill_w = int(round((w_px - 2 * pad) * frac))
        body += f'<rect x="{pad}" y="{bar_y}" width="{fill_w}" height="{bar_h}" rx="10" ry="10" fill="#{accent_hex}" opacity="0.94"/>'
        body += f'<circle cx="{pad + fill_w}" cy="{bar_y + bar_h//2}" r="{max(4, bar_h//2 + 1)}" fill="#{accent_hex}" opacity="0.90"/>'
        if value:
            body += _svg_text(x=w_px - pad, y=int(h_px * 0.28), text=value, size_px=max(12, int(round(h_px * 0.34))), color=f'#{PALETTE["ink"]}', weight=900, anchor="end", baseline="middle")
        return base._svg_root(w_px=w_px, h_px=h_px, body=body)

    def _svg_cell(*, w_px: int, h_px: int, val: str | None, heat: float, accent_hex: str) -> str:
        rr = max(4, int(round(min(w_px, h_px) * 0.18)))
        fill = _mix(PALETTE["card2"], accent_hex, 0.18 + 0.50 * max(0.0, min(1.0, heat)))
        body = f'<rect x="0" y="2" width="{w_px}" height="{h_px}" rx="{rr}" ry="{rr}" fill="#000000" opacity="0.12"/>'
        body += f'<rect x="0" y="0" width="{w_px}" height="{h_px}" rx="{rr}" ry="{rr}" fill="#{fill}" stroke="#{_mix(PALETTE["card_line"], accent_hex, 0.30)}" stroke-width="2"/>'
        body += f'<rect x="2" y="2" width="{max(1, w_px-4)}" height="{max(1, int(h_px*0.26))}" rx="{max(2, rr-2)}" ry="{max(2, rr-2)}" fill="#FFFFFF" opacity="0.06"/>'
        if val:
            size = max(9, int(round(h_px * 0.38)))
            body += _svg_text(x=w_px // 2, y=h_px // 2, text=val, size_px=size, color=f'#{PALETTE["ink"]}', weight=850, anchor="middle")
        return base._svg_root(w_px=w_px, h_px=h_px, body=body)

    def _svg_post_card(*, w_px: int, h_px: int, accent_hex: str, author: str, lines: list[str], rank: int) -> str:
        r = 18
        stripe_w = max(10, int(round(w_px * 0.03)))
        pad = 16
        body = _shadow_rect(x=0, y=0, w=w_px, h=h_px, r=r, fill="#000000", opacity=0.18, dy=7)
        body += f'<rect x="0" y="0" width="{w_px}" height="{h_px}" rx="{r}" ry="{r}" fill="#{PALETTE["card2"]}" stroke="#{PALETTE["card_line"]}" stroke-width="2"/>'
        body += f'<rect x="0" y="0" width="{stripe_w}" height="{h_px}" rx="{r}" ry="{r}" fill="#{accent_hex}" opacity="0.96"/>'
        av_r = max(8, int(round(h_px * 0.11)))
        av_cx = pad + av_r
        av_cy = pad + av_r
        author_size = max(15, int(round(h_px * 0.19)))
        body += f'<circle cx="{av_cx}" cy="{av_cy}" r="{av_r}" fill="#{accent_hex}" opacity="0.96"/>'
        body += f'<circle cx="{av_cx}" cy="{av_cy}" r="{max(2, av_r-3)}" fill="#{PALETTE["card"]}" opacity="0.95"/>'
        body += _svg_text(x=av_cx + av_r + 8, y=av_cy, text=author, size_px=author_size, color=f'#{PALETTE["ink"]}', weight=900)
        sep_y = pad + av_r * 2 + 10
        body += f'<line x1="{pad}" y1="{sep_y}" x2="{w_px-pad}" y2="{sep_y}" stroke="#{PALETTE["card_line"]}" stroke-opacity="0.55" stroke-width="1"/>'
        line_y = sep_y + 18
        line_gap = max(16, int(round(h_px * 0.16)))
        line_size = max(12, int(round(h_px * 0.15)))
        for idx, line in enumerate(lines):
            op = 0.95 if idx == 0 else 0.75
            body += _svg_text(x=pad, y=line_y + idx * line_gap, text=line, size_px=line_size, color=f'#{PALETTE["muted"]}', weight=650, baseline="hanging", opacity=op)
        badge_h = max(20, int(round(h_px * 0.14)))
        badge_w = max(58, int(round(w_px * 0.20)))
        bx = pad
        by = h_px - pad - badge_h
        body += f'<rect x="{bx}" y="{by}" width="{badge_w}" height="{badge_h}" rx="{badge_h//2}" ry="{badge_h//2}" fill="#08111D" stroke="#{accent_hex}" stroke-width="2" opacity="0.98"/>'
        body += _svg_text(x=bx + badge_w // 2, y=by + badge_h // 2, text=f"rank {int(rank)}", size_px=max(11, line_size - 1), color=f'#{PALETTE["ink"]}', weight=850, anchor="middle", baseline="middle")
        bar_w = max(42, int(round(w_px * 0.24)))
        bar_h = max(6, int(round(h_px * 0.08)))
        bar_x = w_px - pad - bar_w
        bar_y = h_px - pad - bar_h - 2
        body += _svg_text(x=bar_x, y=sep_y + 4, text="v(rank)", size_px=max(11, line_size - 1), color=f'#{PALETTE["muted"]}', weight=750, baseline="hanging")
        body += f'<rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="{bar_h}" rx="8" ry="8" fill="#08111D" opacity="0.92"/>'
        frac = 1.0 / (1.0 + 0.18 * (rank - 1))
        body += f'<rect x="{bar_x}" y="{bar_y}" width="{int(round(bar_w * frac))}" height="{bar_h}" rx="8" ry="8" fill="#{accent_hex}" opacity="0.90"/>'
        return base._svg_root(w_px=w_px, h_px=h_px, body=body)

    def _svg_cluster_bubble(*, w_px: int, h_px: int, label: str, accent_hex: str) -> str:
        cx = w_px // 2
        cy = h_px // 2
        r = min(w_px, h_px) // 2 - 10
        body = f'<circle cx="{cx}" cy="{cy+6}" r="{r}" fill="#000000" opacity="0.18"/>'
        body += f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#{_mix(PALETTE["card2"], accent_hex, 0.12)}" stroke="#{accent_hex}" stroke-width="4" opacity="0.98"/>'
        body += f'<circle cx="{cx}" cy="{cy}" r="{max(8, r-12)}" fill="none" stroke="#{accent_hex}" stroke-width="3" stroke-dasharray="8 8" opacity="0.55"/>'
        badge_w = max(48, int(round(w_px * 0.34)))
        badge_h = max(18, int(round(h_px * 0.13)))
        bx = cx - badge_w // 2
        by = cy - r + 18
        body += f'<rect x="{bx}" y="{by}" width="{badge_w}" height="{badge_h}" rx="9" ry="9" fill="#08111D" stroke="#{PALETTE["card_line"]}" stroke-width="2" opacity="0.96"/>'
        body += _svg_text(x=cx, y=by + badge_h // 2, text="# hash", size_px=11, color=f'#{PALETTE["muted"]}', weight=800, anchor="middle")
        body += _svg_text(x=cx, y=cy - 8, text=label, size_px=18, color=f'#{PALETTE["ink"]}', weight=900, anchor="middle")
        body += _svg_text(x=cx, y=cy + 16, text="same / near-same text", size_px=13, color=f'#{PALETTE["muted"]}', weight=700, anchor="middle")
        body += _svg_text(x=cx, y=cy + 36, text="+ similar time (optional)", size_px=12, color=f'#{PALETTE["muted"]}', weight=650, anchor="middle", opacity=0.92)
        return base._svg_root(w_px=w_px, h_px=h_px, body=body)

    def _svg_timeline_card(*, w_px: int, h_px: int, accent_hex: str) -> str:
        pad = 18
        x0 = pad
        y0 = 64
        x1 = w_px - pad
        y1 = h_px - pad
        xs = [x0, x0 + int((x1-x0)*0.25), x0 + int((x1-x0)*0.60), x1]
        ys_a = [y1-4, y1-42, y1-84, y0+28]
        ys_r = [y1-4, y1-18, y1-52, y0+40]
        poly_a = " ".join(f"{x},{y}" for x, y in zip(xs, ys_a))
        poly_r = " ".join(f"{x},{y}" for x, y in zip(xs, ys_r))
        body = _shadow_rect(x=0, y=0, w=w_px, h=h_px, r=18, fill="#000000", opacity=0.18, dy=8)
        body += f'<rect x="0" y="0" width="{w_px}" height="{h_px}" rx="18" ry="18" fill="#{PALETTE["card2"]}" stroke="#{PALETTE["card_line"]}" stroke-width="2"/>'
        body += _svg_text(x=pad, y=pad + 4, text="Amortized attention over snapshots", size_px=14, color=f'#{PALETTE["ink"]}', weight=900, baseline="hanging")
        body += _svg_text(x=pad, y=pad + 28, text="A(p)=Σ_t v(rank_t(p))   R(p)=Σ_t u(p|q_t)", size_px=12, color=f'#{PALETTE["muted"]}', weight=700, baseline="hanging")
        body += f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="#{PALETTE["card_line"]}" stroke-width="2" opacity="0.8"/>'
        body += f'<line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y1}" stroke="#{PALETTE["card_line"]}" stroke-width="2" opacity="0.8"/>'
        for i, tx in enumerate(xs):
            body += f'<line x1="{tx}" y1="{y1}" x2="{tx}" y2="{y1+6}" stroke="#{PALETTE["card_line"]}" stroke-width="2" opacity="0.65"/>'
            body += _svg_text(x=tx, y=y1 + 10, text=f"t{i+1}" if i < len(xs)-1 else "t6", size_px=10, color=f'#{PALETTE["muted"]}', weight=700, anchor="middle", baseline="hanging")
        body += f'<polygon points="{poly_a} {x1},{y1} {x0},{y1}" fill="#{accent_hex}" opacity="0.12"/>'
        body += f'<polyline fill="none" stroke="#{accent_hex}" stroke-width="4" opacity="0.95" points="{poly_a}"/>'
        for x, y in zip(xs, ys_a):
            body += f'<circle cx="{x}" cy="{y}" r="4" fill="#{accent_hex}" opacity="0.92"/>'
        body += f'<polyline fill="none" stroke="#{PALETTE["muted"]}" stroke-width="3" opacity="0.85" points="{poly_r}"/>'
        for x, y in zip(xs, ys_r):
            body += f'<circle cx="{x}" cy="{y}" r="3" fill="#{PALETTE["muted"]}" opacity="0.85"/>'
        body += _svg_text(x=x0 + 6, y=y0 + 10, text="A(p)", size_px=13, color=f'#{accent_hex}', weight=900, baseline="hanging")
        body += _svg_text(x=x0 + 56, y=y0 + 10, text="R(p)", size_px=13, color=f'#{PALETTE["muted"]}', weight=900, baseline="hanging")
        return base._svg_root(w_px=w_px, h_px=h_px, body=body)

    def _wrap_line(text: str, max_chars: int) -> list[str]:
        words = text.split()
        if not words or max_chars <= 8:
            return [text]
        lines: list[str] = []
        current = words[0]
        for word in words[1:]:
            test = f"{current} {word}"
            if len(test) <= max_chars:
                current = test
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    def _svg_caption(*, w_px: int, h_px: int, lines: list[str], accent_hex: str | None = None) -> str:
        pad = 18
        body = _shadow_rect(x=0, y=0, w=w_px, h=h_px, r=16, fill="#000000", opacity=0.18, dy=6)
        body += f'<rect x="0" y="0" width="{w_px}" height="{h_px}" rx="16" ry="16" fill="#{PALETTE["card2"]}" stroke="#{PALETTE["card_line"]}" stroke-width="2"/>'
        if accent_hex:
            body += f'<rect x="0" y="0" width="10" height="{h_px}" rx="16" ry="16" fill="#{accent_hex}" opacity="0.22"/>'
        body += f'<line x1="0" y1="1" x2="{w_px}" y2="1" stroke="#FFFFFF" stroke-opacity="0.08" stroke-width="2"/>'

        if not lines:
            return base._svg_root(w_px=w_px, h_px=h_px, body=body)

        header_size = 15
        body_size = 13
        header_max = max(16, int((w_px - 2 * pad) / 8.5))
        body_max = max(18, int((w_px - 2 * pad - 10) / 7.6))
        header_lines = _wrap_line(lines[0], header_max)
        body_lines: list[str] = []
        for line in lines[1:]:
            text = line.lstrip("\u2022 ").strip()
            body_lines.extend(_wrap_line(text, body_max))

        line_gap = 18
        total_lines = len(header_lines) + len(body_lines)
        while total_lines and (pad + len(header_lines) * 18 + len(body_lines) * line_gap + 12) > h_px and body_size > 11:
            body_size -= 1
            line_gap = max(14, int(round(body_size * 1.20)))

        body += _svg_multiline(
            x=pad,
            y=pad + 2,
            lines=header_lines,
            size_px=header_size,
            color=f'#{PALETTE["ink"]}',
            weight=900,
            line_gap_px=17,
            baseline="hanging",
        )

        y0 = pad + len(header_lines) * 17 + 12
        for i, line in enumerate(body_lines):
            y = y0 + i * line_gap
            body += f'<circle cx="{pad}" cy="{y + 7}" r="3" fill="#{accent_hex or PALETTE["muted"]}" opacity="0.92"/>'
            body += _svg_text(
                x=pad + 10,
                y=y,
                text=line,
                size_px=body_size,
                color=f'#{PALETTE["muted"]}',
                weight=650,
                baseline="hanging",
            )
        return base._svg_root(w_px=w_px, h_px=h_px, body=body)

    def _svg_join_graph(*, w_px: int, h_px: int, accent_hex: str) -> str:
        return base._svg_join_graph.__wrapped__(w_px=w_px, h_px=h_px, accent_hex=accent_hex)  # type: ignore[attr-defined]

    # Preserve original for one helper we don't want to rewrite.
    if not hasattr(base._svg_join_graph, "__wrapped__"):
        base._svg_join_graph.__wrapped__ = base._svg_join_graph  # type: ignore[attr-defined]

    base._svg_text = _svg_text
    base._svg_multiline = _svg_multiline
    base._svg_grid_bg = _svg_grid_bg
    base._svg_title_block = _svg_title_block
    base._svg_panel_label = _svg_panel_label
    base._svg_pill = _svg_pill
    base._svg_chip = _svg_chip
    base._svg_rank_row = _svg_rank_row
    base._svg_metric_card_bg = _svg_metric_card_bg
    base._svg_bar_row = _svg_bar_row
    base._svg_scalar_bar = _svg_scalar_bar
    base._svg_cell = _svg_cell
    base._svg_post_card = _svg_post_card
    base._svg_cluster_bubble = _svg_cluster_bubble
    base._svg_timeline_card = _svg_timeline_card
    base._svg_caption = _svg_caption


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a visually enhanced two-slide IFX poster deck.")
    parser.add_argument("--out-pre", type=Path, default=Path("_build/ifx_poster_enhanced_preanim.pptx"))
    parser.add_argument("--out-svg", type=Path, default=Path("_build/ifx_poster_enhanced_svg.pptx"))
    parser.add_argument("--out", type=Path, default=Path("_build/ifx_poster_enhanced_2slides_raw.pptx"))
    parser.add_argument("--dur-ms", type=int, default=240)
    parser.add_argument("--keep-intermediates", action="store_true")
    args = parser.parse_args()

    _patch_theme()
    base.build_deck(
        out_pre=args.out_pre,
        out_svg=args.out_svg,
        out_animated=args.out,
        effect_dur_ms=int(args.dur_ms),
        keep_intermediates=bool(args.keep_intermediates),
    )


if __name__ == "__main__":
    main()
