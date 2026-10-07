"""Pilot pack: fill the business's details into the document templates.

    countvision-edge pilot-docs --info pilot.yaml --out pilot_docs
    countvision-edge pilot-docs --blank --out pilot_docs      (empty lines to fill in by hand)

The result is a folder of HTML files. Open one in the browser and print it (Ctrl+P), or
"Save as PDF". Everything is a TEMPLATE, not legal advice: have it checked before real use.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..errors import ConfigError

BLANK = "<span class='blank'>&nbsp;</span>"
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_.]+)\s*\}\}")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Party(_Model):
    name: str = ""
    address: str = ""
    contact_person: str = ""
    email: str = ""
    phone: str = ""


class Business(Party):
    dpo: str = ""  # data protection officer (Datenschutzbeauftragte/r), if the business has one
    works_council: bool = False  # Betriebsrat exists


class Camera(_Model):
    name: str
    location: str = ""  # where it hangs and what it sees
    counts: str = "Personen (Ein-/Ausgang)"


class PilotInfo(_Model):
    """Everything the templates need. Empty fields become lines to fill in by hand."""

    business: Business = Field(default_factory=Business)
    provider: Party = Field(default_factory=Party)
    site_address: str = ""
    cameras: list[Camera] = Field(default_factory=list)
    start: str = ""  # e.g. 2026-11-01
    end: str = ""
    purposes: list[str] = Field(default_factory=lambda: [
        "Personaleinsatz und Öffnungszeiten nach dem tatsächlichen Besucheraufkommen planen",
        "Wartezeiten an der Kasse verkürzen",
        "Höchstzahl gleichzeitig anwesender Personen einhalten (Sicherheit)",
    ])
    counts_retention: str = (
        "höchstens 45 Tage auf dem Zählgerät, danach automatische Löschung; "
        "Tages- und Wochensummen erhält der Betrieb als Bericht"
    )
    authority: str = (
        "Landesbeauftragte für Datenschutz und Informationsfreiheit Nordrhein-Westfalen (LDI NRW), "
        "Kavalleriestraße 2-4, 40213 Düsseldorf"
    )
    city: str = ""


@dataclass(frozen=True)
class Document:
    file: str
    title: str
    audience: str


DOCUMENTS = [
    Document("hinweisschild.html", "Hinweisschild (A4, DE/EN)",
             "an jedem Eingang / im Sichtbereich aushängen"),
    Document("datenschutzhinweis.html", "Datenschutzhinweis nach Art. 13 DSGVO (DE/EN)",
             "an der Kasse auslegen und auf der Website veröffentlichen"),
    Document("pilotvereinbarung.html", "Pilotvereinbarung (Einverständnis des Betriebs)",
             "beide unterschreiben"),
    Document("avv.html", "Auftragsverarbeitungsvertrag (Art. 28 DSGVO) mit TOM",
             "beide unterschreiben"),
    Document("dsfa.html", "Datenschutz-Folgenabschätzung – Kurzvorlage",
             "Betrieb (Verantwortlicher) füllt aus"),
    Document("checkliste.html", "Checkliste vor dem Start", "Anbieter und Betrieb gemeinsam"),
]


def load_info(path: str | Path) -> PilotInfo:
    file = Path(path)
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        return PilotInfo.model_validate(data)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise ConfigError(f"Bad pilot info file {file}: {exc}") from exc


def _context(info: PilotInfo) -> dict[str, str]:
    """Flat, HTML-escaped values. Empty values become a blank line to write on."""

    def val(text: str) -> str:
        return html.escape(text).replace("\n", "<br>") if text.strip() else BLANK

    ctx: dict[str, str] = {}
    for prefix, party in (("business", info.business), ("provider", info.provider)):
        for key, value in party.model_dump().items():
            if isinstance(value, str):
                ctx[f"{prefix}.{key}"] = val(value)
    ctx["business.dpo"] = val(info.business.dpo) if info.business.dpo.strip() else (
        "Nicht benannt / not appointed" if info.business.name.strip() else BLANK)
    ctx["business.works_council"] = "Ja" if info.business.works_council else "Nein / ________"
    for key in ("site_address", "start", "end", "counts_retention", "authority", "city"):
        ctx[key] = val(getattr(info, key))
    ctx["purposes"] = "".join(f"<li>{html.escape(p)}</li>" for p in info.purposes) or f"<li>{BLANK}</li>"
    ctx["purposes_short"] = html.escape("; ".join(info.purposes)) if info.purposes else BLANK
    rows = [
        f"<tr><td>{html.escape(c.name)}</td><td>{val(c.location)}</td><td>{html.escape(c.counts)}</td></tr>"
        for c in info.cameras
    ] or [f"<tr><td>{BLANK}</td><td>{BLANK}</td><td>{BLANK}</td></tr>" for _ in range(3)]
    ctx["cameras_rows"] = "\n".join(rows)
    ctx["camera_count"] = str(len(info.cameras)) if info.cameras else BLANK
    ctx["today"] = date.today().strftime("%d.%m.%Y")
    return ctx


def render(template: str, ctx: dict[str, str]) -> str:
    """Replace {{ key }}. An unknown key is an error (a typo must not reach a customer)."""

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in ctx:
            raise ConfigError(f"Template uses an unknown field: {key}")
        return ctx[key]

    return _PLACEHOLDER.sub(repl, template)


def template_text(name: str) -> str:
    return resources.files("countvision_edge.pilot").joinpath("templates", name).read_text(encoding="utf-8")


def write_pack(info: PilotInfo, out_dir: str | Path) -> list[Path]:
    """Write all documents plus an index page. Returns the written files."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ctx = _context(info)
    css = template_text("style.css")
    written: list[Path] = []
    for doc in DOCUMENTS:
        body = render(template_text(doc.file), ctx)
        page = body.replace("<!--STYLE-->", f"<style>\n{css}\n</style>")
        path = out / doc.file
        path.write_text(page, encoding="utf-8")
        written.append(path)
    items = "\n".join(
        f"<li><a href='{d.file}'>{html.escape(d.title)}</a> – {html.escape(d.audience)}</li>"
        for d in DOCUMENTS
    )
    name = info.business.name or "(noch kein Betrieb eingetragen)"
    index = (
        "<!doctype html><html lang='de'><head><meta charset='utf-8'><title>Pilot-Unterlagen</title>"
        f"<style>{css}</style></head><body><main class='page'>"
        f"<h1>CountVision – Pilot-Unterlagen</h1><p><b>Betrieb:</b> {html.escape(name)}</p>"
        "<p class='warn'>Vorlagen, keine Rechtsberatung. Vor dem Einsatz von einer Rechtsanwältin / "
        "einem Rechtsanwalt oder einer/einem Datenschutzbeauftragten prüfen lassen.</p>"
        f"<ol>{items}</ol><p>Drucken: Dokument öffnen, Strg+P, „Als PDF speichern“ oder Drucker wählen.</p>"
        "</main></body></html>"
    )
    index_path = out / "index.html"
    index_path.write_text(index, encoding="utf-8")
    return [index_path, *written]

