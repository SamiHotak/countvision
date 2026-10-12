"""The PDF report of a site: A4, calm, printable (white paper, one teal accent).

Built with ReportLab (BSD licence) from the same numbers as the dashboard (analytics.dashboard),
so the PDF and the screen always agree. Font: Barlow (SIL Open Font License, fonts/OFL.txt),
the same font as the web app. Characters Barlow does not have (for example Arabic or Cyrillic
site names) use DejaVu Sans when the system has it, else they are replaced by "?".

Page 1: the numbers a shop owner asks first (visitors, change, busiest hour, visit length),
a traffic chart and the lines. Page 2: weekday x hour pattern, zones, visit lengths, what was
counted, and how complete the data is.
"""

from __future__ import annotations

import io
import logging
import os
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    CondPageBreak,
    Flowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

log = logging.getLogger(__name__)

FONT_DIR = Path(__file__).with_name("fonts")
FALLBACK_FONTS = [
    os.environ.get("CV_PDF_FALLBACK_FONT", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
]

# Colours: printable ink on white, one accent (the app's teal, darker for paper).
INK = colors.HexColor("#14222c")
MUTED = colors.HexColor("#5b6f7a")
FAINT = colors.HexColor("#9aabb4")
RULE = colors.HexColor("#dfe6ea")
SURFACE = colors.HexColor("#f4f7f8")
ACCENT = colors.HexColor("#11998b")
ACCENT_SOFT = colors.HexColor("#bfe7e2")
GOOD = colors.HexColor("#1a8a4a")
BAD = colors.HexColor("#c2463d")
# Sequential teal ramp for the weekday x hour pattern (light = few, dark = many).
RAMP = ["#eef6f5", "#d3ece8", "#a9dbd4", "#78c6bc", "#47ada1", "#218f83", "#0f6f66", "#0a514b"]

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
CLASS_LABELS = {
    "person": "People",
    "bicycle": "Bicycles",
    "car": "Cars",
    "motorcycle": "Motorcycles",
    "bus": "Buses",
    "truck": "Trucks",
    "unknown": "Unknown",
}


# --------------------------------------------------------------------------- fonts


@lru_cache
def fonts() -> dict[str, Any]:
    """Register Barlow (+ a fallback font) once. Returns the font names and Barlow's characters."""
    names = {
        "regular": "Helvetica",
        "medium": "Helvetica-Bold",
        "bold": "Helvetica-Bold",
        "fallback": None,
        "chars": None,
    }
    try:
        for key, file in (
            ("regular", "Barlow-Regular.ttf"),
            ("medium", "Barlow-Medium.ttf"),
            ("bold", "Barlow-SemiBold.ttf"),
        ):
            font = TTFont(f"CV-{key}", str(FONT_DIR / file))
            pdfmetrics.registerFont(font)
            names[key] = f"CV-{key}"
            if key == "regular":
                names["chars"] = set(font.face.charToGlyph.keys())
    except Exception:  # missing font files: the built-in Helvetica still works (Latin only)
        log.warning("Barlow fonts not found in %s, using Helvetica", FONT_DIR, exc_info=True)
    for path in FALLBACK_FONTS:
        if path and Path(path).is_file():
            try:
                pdfmetrics.registerFont(TTFont("CV-fallback", path))
                names["fallback"] = "CV-fallback"
                break
            except Exception:
                log.warning("Could not load fallback font %s", path, exc_info=True)
    return names


def _supported(ch: str) -> bool:
    chars = fonts()["chars"]
    if chars is None:
        return ord(ch) < 256
    return ord(ch) in chars or ch in "\n\t"


def markup(text: str) -> str:
    """Escape text for a Paragraph; characters outside Barlow use the fallback font (or '?')."""
    out: list[str] = []
    fallback = fonts()["fallback"]
    run: list[str] = []
    for ch in str(text):
        if _supported(ch):
            if run:
                out.append(f'<font name="{fallback}">{escape("".join(run))}</font>')
                run = []
            out.append(escape(ch))
        elif fallback:
            run.append(ch)
        else:
            out.append("?")
    if run:
        out.append(f'<font name="{fallback}">{escape("".join(run))}</font>')
    return "".join(out)


def plain(text: str) -> str:
    """Text for canvas.drawString (no markup): unsupported characters become '?'."""
    return "".join(ch if _supported(ch) else "?" for ch in str(text))


def styles() -> dict[str, ParagraphStyle]:
    f = fonts()
    base = ParagraphStyle("base", fontName=f["regular"], fontSize=9.5, leading=13, textColor=INK)
    return {
        "base": base,
        "muted": ParagraphStyle("muted", parent=base, textColor=MUTED),
        "small": ParagraphStyle("small", parent=base, fontSize=8, leading=10.5, textColor=MUTED),
        "brand": ParagraphStyle("brand", parent=base, fontName=f["bold"], fontSize=9, textColor=ACCENT),
        "title": ParagraphStyle("title", parent=base, fontName=f["bold"], fontSize=24, leading=28),
        "h2": ParagraphStyle(
            "h2", parent=base, fontName=f["bold"], fontSize=13, leading=17, spaceBefore=4, spaceAfter=6
        ),
        "kpi": ParagraphStyle("kpi", parent=base, fontName=f["bold"], fontSize=26, leading=29),
        "kpi_label": ParagraphStyle("kpi_label", parent=base, fontSize=9, leading=11.5, textColor=MUTED),
        "cell": ParagraphStyle("cell", parent=base, fontSize=9, leading=11.5),
        "cell_r": ParagraphStyle("cell_r", parent=base, fontSize=9, leading=11.5, alignment=TA_RIGHT),
        "head": ParagraphStyle(
            "head", parent=base, fontName=f["medium"], fontSize=8.5, leading=11, textColor=MUTED
        ),
        "head_r": ParagraphStyle(
            "head_r",
            parent=base,
            fontName=f["medium"],
            fontSize=8.5,
            leading=11,
            textColor=MUTED,
            alignment=TA_RIGHT,
        ),
    }


# --------------------------------------------------------------------------- formatting


def num(value: float | int | None) -> str:
    if value is None:
        return "–"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{int(value):,}"


def pct(value: float | None, signed: bool = True) -> str:
    if value is None:
        return "–"
    if signed:
        return "±0 %" if abs(value) < 0.5 else f"{value:+.0f} %"
    return f"{value:.0f} %"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


def _d(iso: str) -> date:
    return date.fromisoformat(iso)


def period_text(dash: dict[str, Any]) -> str:
    p = dash["period"]
    a, b = _d(p["from"]), _d(p["to"])
    if a == b:
        return a.strftime("%A, %d %B %Y")
    if a.year == b.year:
        return f"{a.strftime('%a %d %b')} to {b.strftime('%a %d %b %Y')}"
    return f"{a.strftime('%d %b %Y')} to {b.strftime('%d %b %Y')}"


def bucket_label(iso: str, bucket: str, days: int) -> str:
    t = datetime.fromisoformat(iso)
    if bucket == "hour":
        return t.strftime("%H") if days == 1 else t.strftime("%a %H")
    return t.strftime("%d.%m") if days > 7 else t.strftime("%a %d")


def peak_text(peak: dict[str, Any] | None, days: int) -> str:
    if not peak:
        return "–"
    t = datetime.fromisoformat(peak["t"])
    hours = f"{t:%H}:00–{(t.hour + 1) % 24:02d}:00"
    return hours if days == 1 else f"{t:%a %d.%m}, {hours}"


# --------------------------------------------------------------------------- charts


class BarChart(Flowable):
    """Visitors per hour/day as thin bars from one baseline; the comparison as a thin line."""

    def __init__(
        self,
        series: list[dict[str, Any]],
        bucket: str,
        days: int,
        compare_label: str,
        width: float,
        height: float = 62 * mm,
        labels: list[str] | None = None,
        legend: str = "Visitors in",
    ) -> None:
        super().__init__()
        self.series, self.bucket, self.days, self.compare_label = series, bucket, days, compare_label
        self.width, self.height = width, height
        self.labels, self.legend = labels, legend

    def wrap(self, *_):
        return self.width, self.height

    def draw(self) -> None:
        c = self.canv
        f = fonts()
        left, bottom, top_pad = 30, 22, 18
        w, h = self.width - left - 4, self.height - bottom - top_pad
        values = [s["in"] or 0 for s in self.series]
        compare = [s["compare_in"] or 0 for s in self.series]
        top = max([*values, *compare, 1])
        step = _nice_step(top)
        top = step * max(1, -(-top // step))
        # grid + y ticks
        c.setFont(f["regular"], 7.5)
        c.setLineWidth(0.5)
        for i in range(int(top // step) + 1):
            y = bottom + h * (i * step) / top
            c.setStrokeColor(RULE)
            c.line(left, y, left + w, y)
            c.setFillColor(FAINT)
            c.drawRightString(left - 5, y - 2.5, _compact(i * step))
        n = len(self.series)
        if n == 0:
            return
        slot = w / n
        bar = min(slot * 0.62, 14)
        c.setFillColor(ACCENT)
        for i, v in enumerate(self.series):
            if v["in"] is None or v["in"] <= 0:
                continue
            bh = h * v["in"] / top
            x = left + i * slot + (slot - bar) / 2
            r = min(1.6, bar / 2, bh)
            c.roundRect(x, bottom, bar, bh, r, stroke=0, fill=1)
            c.rect(x, bottom, bar, min(bh, r), stroke=0, fill=1)  # square at the baseline
        # comparison: thin line through the slot centres
        if any(compare):
            c.setStrokeColor(MUTED)
            c.setLineWidth(1.1)
            path = c.beginPath()
            for i, v in enumerate(compare):
                x, y = left + i * slot + slot / 2, bottom + h * v / top
                path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
            c.drawPath(path, stroke=1, fill=0)
        # x labels: at most ~14 so they never collide
        c.setFillColor(MUTED)
        c.setFont(f["regular"], 7.5)
        every = max(1, -(-n // 14))
        for i, v in enumerate(self.series):
            if i % every == 0:
                c.drawCentredString(
                    left + i * slot + slot / 2,
                    bottom - 11,
                    self.labels[i] if self.labels else bucket_label(v["t"], self.bucket, self.days),
                )
        # legend
        y = self.height - 9
        c.setFillColor(ACCENT)
        c.roundRect(left, y - 1, 9, 7, 1.5, stroke=0, fill=1)
        c.setFillColor(INK)
        c.setFont(f["regular"], 8)
        c.drawString(left + 13, y, self.legend)
        if any(compare):
            x2 = left + 13 + c.stringWidth(self.legend, f["regular"], 8) + 16
            c.setStrokeColor(MUTED)
            c.setLineWidth(1.1)
            c.line(x2, y + 2.5, x2 + 12, y + 2.5)
            c.drawString(x2 + 16, y, plain(self.compare_label.capitalize()))


class Heatmap(Flowable):
    """Average visitors per weekday (rows) and hour (columns), one teal ramp."""

    def __init__(self, heat: dict[str, Any], width: float) -> None:
        super().__init__()
        self.heat, self.width = heat, width
        cells = heat["cells"]
        used = [c["hour"] for c in cells if c["days"] > 0]
        self.h0 = min(used) if used else 0
        self.h1 = max(used) if used else 23
        self.height = 7 * 5.2 * mm + 30

    def wrap(self, *_):
        return self.width, self.height

    def draw(self) -> None:
        c = self.canv
        f = fonts()
        left, bottom = 28, 22
        hours = list(range(self.h0, self.h1 + 1))
        cw = (self.width - left - 4) / max(1, len(hours))
        ch = 5.2 * mm
        top = self.heat["max"] or 1
        grid = {(x["weekday"], x["hour"]): x for x in self.heat["cells"]}
        c.setFont(f["regular"], 7.5)
        for row, wd in enumerate(range(7)):
            y = bottom + (6 - row) * ch
            c.setFillColor(MUTED)
            c.drawString(0, y + ch / 2 - 2.5, WEEKDAYS[wd])
            for col, hr in enumerate(hours):
                cell = grid.get((wd, hr))
                x = left + col * cw
                if not cell or cell["avg_in"] is None:
                    c.setFillColor(colors.white)
                    c.setStrokeColor(RULE)
                    c.setLineWidth(0.4)
                    c.rect(x + 0.6, y + 0.6, cw - 1.2, ch - 1.2, stroke=1, fill=1)
                    continue
                level = (
                    0
                    if cell["avg_in"] <= 0
                    else 1 + min(len(RAMP) - 2, int(cell["avg_in"] / top * (len(RAMP) - 1)))
                )
                c.setFillColor(colors.HexColor(RAMP[level]))
                c.rect(x + 0.6, y + 0.6, cw - 1.2, ch - 1.2, stroke=0, fill=1)
        c.setFillColor(MUTED)
        every = 1 if len(hours) <= 14 else 2
        for col, hr in enumerate(hours):
            if col % every == 0:
                c.drawCentredString(left + col * cw + cw / 2, bottom - 10, f"{hr:02d}")
        # scale legend
        lx, ly = left, 2
        c.drawString(lx, ly, "Fewer")
        x = lx + 26
        for colour in RAMP[1:]:
            c.setFillColor(colors.HexColor(colour))
            c.rect(x, ly - 1, 10, 7, stroke=0, fill=1)
            x += 11
        c.setFillColor(MUTED)
        c.drawString(x + 4, ly, f"More (up to {num(top)} per hour)")


class HBars(Flowable):
    """Small horizontal bars (visit lengths): label, bar, value."""

    def __init__(self, rows: list[tuple[str, float, str]], width: float) -> None:
        super().__init__()
        self.rows, self.width = rows, width
        self.height = len(rows) * 14 + 4

    def wrap(self, *_):
        return self.width, self.height

    def draw(self) -> None:
        c = self.canv
        f = fonts()
        top = max([v for _, v, _ in self.rows] + [1])
        label_w, value_w = 62, 46
        bar_w = self.width - label_w - value_w
        c.setFont(f["regular"], 8)
        for i, (label, value, text) in enumerate(self.rows):
            y = self.height - (i + 1) * 14 + 3
            c.setFillColor(MUTED)
            c.drawString(0, y + 1, plain(label))
            c.setFillColor(SURFACE)
            c.rect(label_w, y, bar_w, 8, stroke=0, fill=1)
            if value > 0:
                c.setFillColor(ACCENT)
                c.roundRect(label_w, y, max(2, bar_w * value / top), 8, 1.5, stroke=0, fill=1)
            c.setFillColor(INK)
            c.drawRightString(self.width, y + 1, plain(text))


def _nice_step(top: float) -> int:
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000, 10000, 20000, 50000):
        if top / step <= 5:
            return step
    return 100000


def _compact(v: float) -> str:
    return f"{v / 1000:g}K" if v >= 1000 else f"{v:g}"


# --------------------------------------------------------------------------- the document


def _table(rows: list[list[Any]], widths: list[float], right_from: int = 1) -> Table:
    st = styles()
    data = []
    for r, row in enumerate(rows):
        out = []
        for i, cell in enumerate(row):
            style = (
                (st["head_r"] if i >= right_from else st["head"])
                if r == 0
                else (st["cell_r"] if i >= right_from else st["cell"])
            )
            out.append(cell if isinstance(cell, Flowable) else Paragraph(markup(str(cell)), style))
        data.append(out)
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
                ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return t


def _kpis(dash: dict[str, Any], width: float) -> Table:
    st = styles()
    t = dash["totals"]
    change = t["change_pct"]
    colour = MUTED if change is None or abs(change) < 0.5 else (GOOD if change > 0 else BAD)
    change_text = pct(change) if change is not None else "–"
    dwell = dash["dwell"]["avg_s"] if dash["dwell"]["count"] else None
    cov = dash["coverage"]["pct"]
    cells = [
        (num(t["in"]), "Visitors in", INK),
        (
            change_text,
            f"vs {dash['compare']['label']} ({num(t['compare_in'])})"
            if t.get("comparable", True)
            else f"not compared: too little data {dash['compare']['label']}",
            colour,
        ),
        (peak_text(dash["peak"], dash["period"]["days"]), "Busiest hour", INK),
        (duration(dwell), "Average visit in zones", INK),
        (
            pct(cov, signed=False) if cov is not None else "–",
            "Data coverage",
            INK if (cov or 0) >= 90 else BAD,
        ),
    ]
    row = []
    for value, label, colour_ in cells:
        size = 26 if len(value) <= 7 else 15
        style = ParagraphStyle("k", parent=st["kpi"], textColor=colour_, fontSize=size, leading=size + 3)
        row.append([Paragraph(markup(value), style), Spacer(1, 3), Paragraph(markup(label), st["kpi_label"])])
    widths = [width * 0.2] * 5
    table = Table([row], colWidths=widths)
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBEFORE", (1, 0), (-1, 0), 0.5, RULE),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
                ("LEFTPADDING", (1, 0), (-1, 0), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return table


def build_pdf(dash: dict[str, Any], org_name: str, now: datetime | None = None) -> bytes:
    """Render the dashboard numbers of one site and period as a PDF (bytes)."""
    st = styles()
    buf = io.BytesIO()
    site = dash["site"]
    tz = ZoneInfo(site["timezone"])
    generated = (now or dash["generated_at"]).astimezone(tz)
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title=f"CountVision report {site['name']}",
        author="CountVision",
        subject=period_text(dash),
        creator="CountVision",
    )
    width = A4[0] - 36 * mm
    f = fonts()

    def footer(canvas, document) -> None:
        canvas.saveState()
        canvas.setFont(f["regular"], 7.5)
        canvas.setFillColor(FAINT)
        canvas.drawString(
            18 * mm,
            10 * mm,
            plain(
                f"CountVision report, {site['name']}, created {generated:%d.%m.%Y %H:%M} "
                f"({site['timezone']}). Only counts are stored: no images, no video."
            ),
        )
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {document.page}")
        canvas.restoreState()

    story: list[Flowable] = [
        Paragraph(markup(f"COUNTVISION  ·  {org_name}"), st["brand"]),
        Spacer(1, 4),
        Paragraph(markup(site["name"]), st["title"]),
        Paragraph(markup(f"{period_text(dash)}, times in {site['timezone']}"), st["muted"]),
        Spacer(1, 12),
        _kpis(dash, width),
        Spacer(1, 16),
    ]

    if not dash["has_data"]:
        story += [
            Paragraph("No counting data for this period yet.", st["h2"]),
            Paragraph("When a camera on this site counts, the numbers appear here.", st["muted"]),
        ]
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
        return buf.getvalue()

    unit = "hour" if dash["period"]["bucket"] == "hour" else "day"
    story += [
        Paragraph(markup(f"Visitors per {unit}"), st["h2"]),
        BarChart(
            dash["series"], dash["period"]["bucket"], dash["period"]["days"], dash["compare"]["label"], width
        ),
        Spacer(1, 14),
    ]

    lines = dash["lines"]
    if lines:
        rows = [["Camera", "Line", "Counts as visitors", "In", "Out", "Change in"]]
        for ln in lines:
            rows.append(
                [
                    ln["camera_name"],
                    ln["line"],
                    "yes" if ln["visitor"] else "no",
                    num(ln["in"]),
                    num(ln["out"]),
                    pct(ln["change_pct"]),
                ]
            )
        story += [
            KeepTogether(
                [
                    Paragraph("Lines", st["h2"]),
                    _table(rows, [w * width for w in (0.24, 0.24, 0.18, 0.11, 0.11, 0.12)], right_from=3),
                ]
            )
        ]

    story += [CondPageBreak(95 * mm)]
    heat = dash["heatmap"]
    if any(c["days"] for c in heat["cells"]):
        story += [
            KeepTogether(
                [
                    Paragraph("When visitors come", st["h2"]),
                    Paragraph(
                        markup(
                            f"Average visitors per hour, {_d(heat['from']):%d.%m.%Y} to "
                            f"{_d(heat['to']):%d.%m.%Y}. Only hours when the cameras were running count. "
                            "White = no data."
                        ),
                        st["small"],
                    ),
                    Spacer(1, 6),
                    Heatmap(heat, width),
                    Spacer(1, 14),
                ]
            )
        ]

    zones = dash["zones"]
    if zones:
        rows = [["Camera", "Zone", "Kind", "Average", "Most at once", "Visits", "Average visit"]]
        for z in zones:
            queue = z["kind"] == "queue"
            rows.append(
                [
                    z["camera_name"],
                    z["zone"],
                    "queue" if queue else "area",
                    num(z["queue_avg"] if queue else z["occ_avg"]),
                    num(z["queue_max"] if queue else z["occ_max"]),
                    num(z["visits"]),
                    duration(z["dwell_avg_s"]),
                ]
            )
        story += [
            KeepTogether(
                [
                    Paragraph("Zones", st["h2"]),
                    Paragraph(
                        "Average = people in the zone (or waiting in the queue) on average "
                        "while the camera ran.",
                        st["small"],
                    ),
                    Spacer(1, 4),
                    _table(rows, [w * width for w in (0.2, 0.18, 0.1, 0.11, 0.14, 0.1, 0.17)], right_from=3),
                    Spacer(1, 14),
                ]
            )
        ]

    dwell = dash["dwell"]
    classes = dash["classes"]
    col_w = (width - 10 * mm) / 2
    left: list[Flowable] = []
    right: list[Flowable] = []
    if dwell["count"]:
        left = [
            Paragraph("Length of visits", st["h2"]),
            Paragraph(
                markup(f"{num(dwell['count'])} visits, median {duration(dwell['median_s'])}"), st["small"]
            ),
            Spacer(1, 6),
            HBars([(b["label"], b["count"], num(b["count"])) for b in dwell["buckets"]], col_w),
        ]
    if classes:
        total = sum(c["in"] for c in classes) or 1
        right = [
            Paragraph("What was counted (in)", st["h2"]),
            Spacer(1, 4),
            HBars(
                [
                    (
                        CLASS_LABELS.get(c["class_name"], c["class_name"]),
                        c["in"],
                        f"{num(c['in'])}  ({c['in'] / total * 100:.0f} %)",
                    )
                    for c in classes
                ],
                col_w,
            ),
        ]
    if left or right:
        t = Table([[left or "", right or ""]], colWidths=[col_w + 10 * mm, col_w])
        t.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        story += [t, Spacer(1, 14)]

    cov = dash["coverage"]
    if cov["cameras"]:
        rows = [["Camera", "Analysed", "Expected", "Coverage"]]
        for c in cov["cameras"]:
            rows.append(
                [
                    c["camera_name"],
                    duration(c["seconds"]),
                    duration(c["expected_s"]),
                    pct(c["pct"], signed=False),
                ]
            )
        story += [
            CondPageBreak(50 * mm),
            KeepTogether(
                [
                    Paragraph("How complete the data is", st["h2"]),
                    Paragraph(
                        "Expected = the time inside the counting hours. Below 100 % means the camera "
                        "or the computer was off for a while: the real numbers are then a bit higher "
                        "than shown.",
                        st["small"],
                    ),
                    Spacer(1, 4),
                    _table(rows, [w * width for w in (0.4, 0.2, 0.2, 0.2)]),
                ]
            ),
        ]

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


# --------------------------------------------------------------------------- video analysis report


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def build_video_pdf(job: Any, result: dict[str, Any], org_name: str) -> bytes:
    """Report of one analysed video file. Times are positions in the video (0:00 = start)."""
    st = styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=18 * mm,
        title=f"CountVision video analysis {job.name}",
        author="CountVision",
        creator="CountVision",
    )
    width = A4[0] - 36 * mm
    f = fonts()
    created = datetime.now().strftime("%d.%m.%Y %H:%M")

    def footer(canvas, document) -> None:
        canvas.saveState()
        canvas.setFont(f["regular"], 7.5)
        canvas.setFillColor(FAINT)
        canvas.drawString(
            18 * mm,
            10 * mm,
            plain(
                f"CountVision video analysis, {job.name}, created {created}. "
                "The uploaded video was deleted after the analysis."
            ),
        )
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {document.page}")
        canvas.restoreState()

    totals = result["totals"]
    kpi = Table(
        [
            [
                [
                    Paragraph(markup(num(totals["in"])), st["kpi"]),
                    Spacer(1, 3),
                    Paragraph("In (all lines)", st["kpi_label"]),
                ],
                [
                    Paragraph(markup(num(totals["out"])), st["kpi"]),
                    Spacer(1, 3),
                    Paragraph("Out (all lines)", st["kpi_label"]),
                ],
                [
                    Paragraph(markup(_clock(job.duration_s)), st["kpi"]),
                    Spacer(1, 3),
                    Paragraph("Video length", st["kpi_label"]),
                ],
                [
                    Paragraph(
                        markup(duration(result["dwell"]["avg_s"])),
                        ParagraphStyle("k2", parent=st["kpi"], fontSize=18, leading=22),
                    ),
                    Spacer(1, 3),
                    Paragraph("Average visit in zones", st["kpi_label"]),
                ],
            ]
        ],
        colWidths=[width / 4] * 4,
    )
    kpi.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBEFORE", (1, 0), (-1, 0), 0.5, RULE),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
                ("LEFTPADDING", (1, 0), (-1, 0), 10),
            ]
        )
    )
    classes = ", ".join(CLASS_LABELS.get(c, c) for c in result.get("classes", []))
    story: list[Flowable] = [
        Paragraph(markup(f"COUNTVISION  ·  {org_name}"), st["brand"]),
        Spacer(1, 4),
        Paragraph(markup(job.name), st["title"]),
        Paragraph(
            markup(f"Video analysis: {job.width}x{job.height}, {job.fps:g} FPS, counted: {classes}"),
            st["muted"],
        ),
        Spacer(1, 12),
        kpi,
        Spacer(1, 16),
    ]
    tl = result.get("timeline", [])
    if tl and len(tl) > 1:
        step = max(1, -(-len(tl) // 60))  # at most ~60 bars
        bars, labels = [], []
        for i in range(0, len(tl), step):
            part = tl[i : i + step]
            bars.append({"t": "", "in": sum(p["in"] + p["out"] for p in part), "compare_in": 0})
            labels.append(_clock(part[0]["minute"] * 60))
        story += [
            Paragraph(
                markup(f"Crossings (in + out) per {'minute' if step == 1 else f'{step} minutes'} of video"),
                st["h2"],
            ),
            BarChart(bars, "minute", 1, "", width, labels=labels, legend="Crossings, all lines"),
            Spacer(1, 14),
        ]
    rows = [["Line", "In", "Out", "By class (in / out)"]]
    for ln in result["lines"]:
        by = ", ".join(
            f"{CLASS_LABELS.get(k, k)} {v['in']}/{v['out']}" for k, v in sorted(ln["classes"].items())
        )
        rows.append([ln["name"], num(ln["in"]), num(ln["out"]), by or "–"])
    if len(rows) > 1:
        story += [
            KeepTogether(
                [
                    Paragraph("Lines", st["h2"]),
                    _table(rows, [w * width for w in (0.3, 0.12, 0.12, 0.46)], right_from=1),
                ]
            ),
            Spacer(1, 14),
        ]
    if result["zones"]:
        rows = [["Zone", "Kind", "Average", "Most at once", "Visits", "Average visit"]]
        for z in result["zones"]:
            q = z["kind"] == "queue"
            rows.append(
                [
                    z["name"],
                    "queue" if q else "area",
                    num(z["queue_avg"] if q else z["occ_avg"]),
                    num(z["queue_max"] if q else z["occ_max"]),
                    num(z["visits"]),
                    duration(z["dwell_avg_s"]),
                ]
            )
        story += [
            KeepTogether(
                [
                    Paragraph("Zones", st["h2"]),
                    _table(rows, [w * width for w in (0.26, 0.12, 0.13, 0.17, 0.13, 0.19)], right_from=2),
                ]
            ),
            Spacer(1, 14),
        ]
    if result["dwell"]["count"]:
        story += [
            KeepTogether(
                [
                    Paragraph("Length of visits", st["h2"]),
                    HBars(
                        [(b["label"], b["count"], num(b["count"])) for b in result["dwell"]["buckets"]],
                        width / 2,
                    ),
                ]
            ),
            Spacer(1, 14),
        ]
    story += [
        Paragraph("How it was counted", st["h2"]),
        Paragraph(
            markup(
                f"Detector: {result.get('detector')}. Frames analysed: {num(result.get('frames_processed'))} "
                f"of {num(result.get('frames_read'))}. "
                f"Processing time: {duration(result.get('processing_s'))}. "
                "Counts depend on the camera angle and the lines: check a part of the annotated video by eye."
            ),
            st["small"],
        ),
    ]
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
