"""Phase 4 A: site dashboard numbers, visitor lines, CSV/PDF export, scheduled report emails."""

from __future__ import annotations

import io
import time
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import last_link, signup
from pypdf import PdfReader
from sqlalchemy import select, update
from test_devices import batch, setup
from test_orgs import add_member

from countvision_cloud.db import session_factory, utcnow
from countvision_cloud.product.models import ReportSchedule, Site
from countvision_cloud.product.schemas import ScheduleDoc
from countvision_cloud.product.services import analytics, reports
from countvision_cloud.saas.models import Role
from countvision_cloud.saas.services import email

BERLIN = ZoneInfo("Europe/Berlin")


def local_ts(day: date, hh: int, mm: int = 0, ss: int = 0) -> int:
    """Epoch seconds of a local Berlin time."""
    return int(datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=BERLIN).timestamp())


def today() -> date:
    return datetime.now(BERLIN).date()


def cov(cam: str, start: int, minutes: int) -> list[dict]:
    return [{"camera_id": cam, "window_start": start + 60 * i, "frames": 600, "seconds": 60} for i in range(minutes)]


def lc(cam: str, ws: int, line: str, cls: str, n_in: int, n_out: int = 0) -> dict:
    return {"camera_id": cam, "window_start": ws, "line": line, "class_name": cls, "in_count": n_in,
            "out_count": n_out}


def cross(eid: str, ts: float, line: str = "entrance", direction: str = "in", cls: str = "person",
          cam: str = "door") -> dict:
    return {"event_id": eid, "camera_id": cam, "kind": "line_cross", "ts": ts, "name": line,
            "class_name": cls, "direction": direction}


def visit(eid: str, ts: float, dwell: float, zone: str = "queue") -> dict:
    return {"event_id": eid, "camera_id": "door", "kind": "zone_visit", "ts": ts, "name": zone,
            "class_name": "person", "dwell_s": dwell}


def history(dev) -> date:
    """Yesterday: 3 in / 1 out at 10:05 (2 people + 1 bicycle), 5 in at 14:30; two zone visits.
    8 days ago (same weekday last week): 4 in at 10:10, cameras ran 10-11 and 14-15 as well."""
    y = today() - timedelta(days=1)
    w10, w14 = local_ts(y, 10, 5), local_ts(y, 14, 30)
    last_week = local_ts(y - timedelta(days=7), 10, 10)
    rows = dict(
        line_counts=[lc("door", w10, "entrance", "*", 3, 1), lc("door", w10, "entrance", "person", 2, 1),
                     lc("door", w10, "entrance", "bicycle", 1, 0), lc("door", w14, "entrance", "*", 5),
                     lc("door", w14, "entrance", "person", 5), lc("door", last_week, "entrance", "*", 4),
                     lc("door", last_week, "entrance", "person", 4)],
        coverage=cov("door", local_ts(y, 10), 60) + cov("door", local_ts(y, 14), 60)
        + cov("door", local_ts(y - timedelta(days=7), 10), 60) + cov("door", local_ts(y - timedelta(days=7), 14), 60),
        zone_stats=[{"camera_id": "door", "window_start": w10, "zone": "queue", "class_name": "*", "sample_s": 60,
                     "occ_avg": 1.5, "occ_max": 3, "occ_last": 1, "queue_avg": 0.5, "queue_max": 2, "visits": 2,
                     "dwell_sum_s": 245, "dwell_max_s": 200}],
        events=[visit("v1", w10 + 20, 45), visit("v2", w10 + 40, 200)],
    )
    resp = dev.post("/api/device/ingest", json=batch("hist", **rows))
    assert resp.status_code == 200 and resp.json()["rejected"] == 0, resp.text
    return y


def dash(client, org, site, first: date, last: date | None = None) -> dict:
    resp = client.get(f"/api/orgs/{org['id']}/sites/{site['id']}/dashboard",
                      params={"from": first.isoformat(), "to": (last or first).isoformat()})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_dashboard_numbers_for_a_past_day(app, client):
    org, site, code, dev, body = setup(client, app)
    y = history(dev)
    d = dash(client, org, site, y)

    assert d["period"] == {**d["period"], "from": y.isoformat(), "days": 1, "bucket": "hour", "live": False}
    assert d["totals"]["in"] == 8 and d["totals"]["out"] == 1
    assert d["totals"]["compare_in"] == 4 and d["totals"]["change_pct"] == 100.0
    assert d["compare"]["label"] == "last week" and d["compare"]["shift_days"] == 7
    assert len(d["series"]) == 24
    by_hour = {s["t"][-5:]: s for s in d["series"]}
    assert by_hour["10:00"]["in"] == 3 and by_hour["10:00"]["compare_in"] == 4
    assert by_hour["14:00"]["in"] == 5 and by_hour["09:00"]["in"] == 0
    assert d["peak"] == {"t": f"{y.isoformat()}T14:00", "in": 5}
    assert {c["class_name"]: c["in"] for c in d["classes"]} == {"person": 7, "bicycle": 1}
    assert d["lines"][0]["line"] == "entrance" and d["lines"][0]["visitor"] is True

    assert d["dwell"]["count"] == 2 and d["dwell"]["avg_s"] == 122.5
    assert {b["label"]: b["count"] for b in d["dwell"]["buckets"]}["30-60 s"] == 1
    assert {b["label"]: b["count"] for b in d["dwell"]["buckets"]}["2-5 min"] == 1
    zone = d["zones"][0]
    assert (zone["zone"], zone["occ_avg"], zone["occ_max"], zone["visits"], zone["dwell_avg_s"]) == \
        ("queue", 1.5, 3, 2, 122.5)

    # 2 hours analysed of a 24-hour day (no counting hours set)
    assert d["coverage"]["cameras"][0]["seconds"] == 7200
    assert d["coverage"]["pct"] == pytest.approx(8.3, abs=0.1)

    # weekday x hour: yesterday 3 at 10:00 + last week 4 at 10:00 -> average 3.5 on that weekday;
    # 14:00: 5 yesterday, 0 last week (camera ran) -> 2.5
    cell = next(c for c in d["heatmap"]["cells"] if c["weekday"] == y.weekday() and c["hour"] == 10)
    assert cell == {"weekday": y.weekday(), "hour": 10, "days": 2, "avg_in": 3.5}
    assert next(c for c in d["heatmap"]["cells"] if c["weekday"] == y.weekday() and c["hour"] == 14)["avg_in"] == 2.5

    # a 7-day period uses day buckets
    week = dash(client, org, site, y - timedelta(days=6), y)
    assert week["period"]["bucket"] == "day" and len(week["series"]) == 7
    assert week["series"][-1]["in"] == 8 and week["totals"]["in"] == 8


def test_live_tail_counts_new_crossings_once(app, client):
    """Finished minutes come from minute rows, the newest seconds from single events: no gaps, no doubles."""
    org, site, code, dev, body = setup(client, app)
    now = int(time.time())
    m_old = now - now % 60 - 600  # 10 minutes ago, analysed
    m_new = now - now % 60 - 60   # last minute: its coverage row is not uploaded yet
    rows = dict(
        coverage=cov("door", m_old - 240, 5),  # up to m_old + 60
        line_counts=[lc("door", m_old, "entrance", "*", 2), lc("door", m_old, "entrance", "person", 2),
                     lc("door", m_new, "entrance", "*", 1), lc("door", m_new, "entrance", "person", 1)],
        events=[cross("a", m_old + 5), cross("b", m_old + 9),  # already in the minute row
                cross("c", m_new + 3), cross("d", now - 1, direction="out")],  # newer than the coverage
    )
    dev.post("/api/device/ingest", json=batch("tail", **rows))
    first = datetime.fromtimestamp(m_old, BERLIN).date()
    d = dash(client, org, site, first, today())
    assert d["period"]["live"] is True
    assert (d["totals"]["in"], d["totals"]["out"]) == (3, 1)
    assert d["totals"]["inside_estimate"] == 2
    assert {c["class_name"]: c["in"] for c in d["classes"]} == {"person": 3}


def test_visitor_lines_choose_what_counts(app, client, client2):
    org, site, code, dev, body = setup(client, app)
    y = today() - timedelta(days=1)
    w = local_ts(y, 12)
    dev.post("/api/device/ingest", json=batch("two", line_counts=[
        lc("door", w, "entrance", "*", 10), lc("door", w, "aisle", "*", 30)], coverage=cov("door", w, 1)))
    assert dash(client, org, site, y)["totals"]["in"] == 40  # default: every line

    cam = client.get(f"/api/orgs/{org['id']}/devices/{body['device_id']}").json()["cameras"][0]["id"]
    resp = client.put(f"/api/orgs/{org['id']}/sites/{site['id']}/visitor-lines",
                      json={"lines": [{"camera_id": cam, "line": "entrance"}]})
    assert resp.status_code == 200, resp.text
    d = dash(client, org, site, y)
    assert d["totals"]["in"] == 10
    assert {ln["line"]: ln["visitor"] for ln in d["lines"]} == {"entrance": True, "aisle": False}
    assert {ln["line"]: ln["in"] for ln in d["lines"]} == {"entrance": 10, "aisle": 30}

    # viewers read, but cannot change it
    signup(client2, "viewer@example.com", "Vera")
    add_member(org["id"], "viewer@example.com", Role.VIEWER)
    assert client2.get(f"/api/orgs/{org['id']}/sites/{site['id']}/dashboard").status_code == 200
    assert client2.put(f"/api/orgs/{org['id']}/sites/{site['id']}/visitor-lines",
                       json={"lines": None}).status_code == 403

    client.put(f"/api/orgs/{org['id']}/sites/{site['id']}/visitor-lines", json={"lines": None})
    assert dash(client, org, site, y)["totals"]["in"] == 40
    actions = [e["action"] for e in client.get(f"/api/orgs/{org['id']}/audit").json()]
    assert actions.count("site.visitor_lines_changed") == 2


def test_period_checks_and_other_org(app, client, client2):
    org, site, code, dev, body = setup(client, app)
    url = f"/api/orgs/{org['id']}/sites/{site['id']}/dashboard"
    t = today()
    assert client.get(url, params={"from": t.isoformat(), "to": (t - timedelta(days=1)).isoformat()}).status_code == 422
    assert client.get(url, params={"from": (t + timedelta(days=2)).isoformat()}).status_code == 422
    assert client.get(url, params={"from": (t - timedelta(days=400)).isoformat(), "to": t.isoformat()}).status_code == 422
    empty = client.get(url).json()  # default = today, no data yet
    assert empty["period"]["from"] == t.isoformat() and empty["has_data"] is False and empty["totals"]["in"] == 0
    signup(client2, "other@example.com", "Otto")
    assert client2.get(url).status_code == 404


def test_csv_exports(app, client):
    org, site, code, dev, body = setup(client, app)
    y = history(dev)
    base = f"/api/orgs/{org['id']}/sites/{site['id']}"
    resp = client.get(f"{base}/export.csv", params={"from": y.isoformat(), "delimiter": ";"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert f'countvision_shop-mitte_{y.isoformat()}_lines.csv' in resp.headers["content-disposition"]
    text = resp.content.decode("utf-8")
    assert text.startswith("﻿")
    rows = [r.split(";") for r in text.lstrip("﻿").strip().split("\r\n")]
    assert rows[0] == ["date", "time", "camera", "line", "counts_as_visitors", "in", "out", "in_bicycle",
                       "out_bicycle", "in_person", "out_person"]
    assert rows[1] == [y.isoformat(), "10:00", "Entrance", "entrance", "yes", "3", "1", "1", "0", "2", "1"]
    assert rows[2][1:6] == ["14:00", "Entrance", "entrance", "yes", "5"]

    zones = client.get(f"{base}/export.csv", params={"from": y.isoformat(), "kind": "zones", "delimiter": ";",
                                                     "bucket": "day"}).content.decode()
    zrows = [r.split(";") for r in zones.lstrip("﻿").strip().split("\r\n")]
    assert zrows[0][:4] == ["date", "camera", "zone", "kind"]
    assert zrows[1][:6] == [y.isoformat(), "Entrance", "queue", "area", "1,5", "3"]  # decimal comma for ";"

    minutes = client.get(f"{base}/export.csv", params={"from": (y - timedelta(days=10)).isoformat(),
                                                       "to": y.isoformat(), "bucket": "minute"})
    assert minutes.status_code == 422


def test_pdf_report(app, client):
    org, site, code, dev, body = setup(client, app)
    y = history(dev)
    resp = client.get(f"/api/orgs/{org['id']}/sites/{site['id']}/report.pdf", params={"from": y.isoformat()})
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")
    reader = PdfReader(io.BytesIO(resp.content))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert len(reader.pages) == 2
    for word in ("Shop Mitte", "Café Sonne", "Visitors in", "Busiest hour", "+100 %", "14:00–15:00",
                 "Lines", "When visitors come", "Zones", "Length of visits", "How complete the data is"):
        assert word in text, word

    # a name with letters Barlow does not have still renders (fallback font or "?")
    with session_factory()() as db:
        db.execute(update(Site).values(name="Herat دکان"))
        db.commit()
    other = client.get(f"/api/orgs/{org['id']}/sites/{site['id']}/report.pdf", params={"from": y.isoformat()})
    assert other.status_code == 200 and "Herat" in PdfReader(io.BytesIO(other.content)).pages[0].extract_text()

    # an empty period gives a short, honest PDF
    empty = client.get(f"/api/orgs/{org['id']}/sites/{site['id']}/report.pdf",
                       params={"from": (y - timedelta(days=30)).isoformat()})
    assert "No counting data" in PdfReader(io.BytesIO(empty.content)).pages[0].extract_text()


def test_org_overview(app, client):
    org, site, code, dev, body = setup(client, app)
    now = int(time.time())
    dev.post("/api/device/ingest", json=batch("now", events=[cross("x1", now - 5), cross("x2", now - 3)]))
    sites = client.get(f"/api/orgs/{org['id']}/dashboard").json()["sites"]
    assert len(sites) == 1 and sites[0]["name"] == "Shop Mitte"
    if datetime.fromtimestamp(now - 5, BERLIN).date() == today():  # not right after midnight
        assert sites[0]["today_in"] == 2 and sites[0]["camera_count"] == 1
    assert len(sites[0]["hours"]) == 24


# --------------------------------------------------------------------------- pure logic

def at(y, mo, d, h, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=BERLIN)


def test_send_times():
    # daily at 07:00: before 7 the newest report is yesterday's (about the day before)
    due = reports.latest_send("daily", 0, 7, "Europe/Berlin", at(2026, 10, 10, 6, 59))
    assert (due.send_at, due.first, due.last) == (at(2026, 10, 9, 7), date(2026, 10, 8), date(2026, 10, 8))
    due = reports.latest_send("daily", 0, 7, "Europe/Berlin", at(2026, 10, 10, 7, 0))
    assert due.last == date(2026, 10, 9)
    # weekly on Monday: the week Monday..Sunday before. 2026-10-10 is a Saturday.
    due = reports.latest_send("weekly", 0, 7, "Europe/Berlin", at(2026, 10, 10, 12))
    assert (due.send_at.date(), due.first, due.last) == (date(2026, 10, 5), date(2026, 9, 28), date(2026, 10, 4))
    due = reports.latest_send("weekly", 5, 18, "Europe/Berlin", at(2026, 10, 10, 18))  # Saturday 18:00
    assert (due.first, due.last) == (date(2026, 10, 3), date(2026, 10, 9))
    # monthly on the 1st
    due = reports.latest_send("monthly", 0, 7, "Europe/Berlin", at(2026, 3, 1, 6))
    assert (due.first, due.last) == (date(2026, 1, 1), date(2026, 1, 31))
    due = reports.latest_send("monthly", 0, 7, "Europe/Berlin", at(2026, 3, 1, 8))
    assert (due.first, due.last) == (date(2026, 2, 1), date(2026, 2, 28))
    # next send time, across the daylight saving change (25 Oct 2026)
    nxt = reports.next_send("daily", 0, 7, "Europe/Berlin", at(2026, 10, 24, 8))
    assert nxt == at(2026, 10, 25, 7) and nxt.utcoffset() == timedelta(hours=1)
    assert reports.next_send("monthly", 0, 7, "Europe/Berlin", at(2026, 10, 10, 8)) == at(2026, 11, 1, 7)


def test_comparison_and_expected_seconds():
    p = analytics.Period(date(2026, 10, 10), date(2026, 10, 10), "Europe/Berlin")
    live = analytics.comparison(p, at(2026, 10, 10, 14, 10))
    assert live.label == "same time last week"
    assert live.start == at(2026, 10, 3, 0) and live.end == at(2026, 10, 3, 14, 10)
    month = analytics.Period(date(2026, 9, 1), date(2026, 9, 30), "Europe/Berlin")
    cmp = analytics.comparison(month, at(2026, 10, 10, 12))
    assert cmp.label == "previous 30 days" and cmp.first == date(2026, 8, 2) and cmp.last == date(2026, 8, 31)

    day = (at(2026, 10, 5, 0), at(2026, 10, 6, 0))  # a Monday
    assert analytics.expected_seconds(*day, "Europe/Berlin", None) == 86400
    shop = ScheduleDoc(days=[0, 1, 2, 3, 4], start="09:00", end="18:00")
    assert analytics.expected_seconds(*day, "Europe/Berlin", shop) == 9 * 3600
    bar = ScheduleDoc(days=[6], start="20:00", end="02:00")  # Sunday night until Monday 02:00
    assert analytics.expected_seconds(*day, "Europe/Berlin", bar) == 2 * 3600


# --------------------------------------------------------------------------- report emails

def verified(client, addr: str, name: str) -> None:
    signup(client, addr, name)
    token = last_link(addr, "verify-email")
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 200


def schedule_setup(app, client, client2):
    org, site, code, dev, body = setup(client, app)
    client.post("/api/auth/verify-email", json={"token": last_link("owner@example.com", "verify-email")})
    verified(client2, "boss@example.com", "Bernd")
    add_member(org["id"], "boss@example.com", Role.VIEWER)
    members = {m["email"]: m["user_id"] for m in client.get(f"/api/orgs/{org['id']}/members").json()}
    return org, site, dev, members


def test_report_schedule_crud_and_send_now(app, client, client2):
    org, site, dev, members = schedule_setup(app, client, client2)
    history(dev)
    base = f"/api/orgs/{org['id']}/report-schedules"
    bad = client.post(base, json={"site_id": site["id"], "frequency": "weekly",
                                  "recipients": ["00000000-0000-0000-0000-000000000001"]})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "bad_recipient"
    resp = client.post(base, json={"site_id": site["id"], "frequency": "daily", "hour": 7, "attach_csv": True,
                                   "recipients": [members["owner@example.com"], members["boss@example.com"]]})
    assert resp.status_code == 201, resp.text
    s = resp.json()
    assert s["site_name"] == "Shop Mitte" and len(s["recipients"]) == 2 and s["next_send_at"]
    assert s["next_period"]["from"] == s["next_period"]["to"]

    # viewers see the list but cannot change it
    assert client2.get(base).status_code == 200
    assert client2.post(f"{base}/{s['id']}/send").status_code == 403

    email.SENT.clear()
    sent = client.post(f"{base}/{s['id']}/send")
    assert sent.status_code == 200 and sent.json()["emails"] == 2, sent.text
    assert sorted(m.to for m in email.SENT) == ["boss@example.com", "owner@example.com"]
    mail = email.SENT[0]
    assert "Shop Mitte: daily report" in mail.subject and "Visitors in" in mail.text
    assert "/sites/" in mail.text  # link to the dashboard
    names = [a.filename for a in mail.attachments]
    assert names[0].endswith("_report.pdf") and names[1].endswith("_lines.csv")
    assert mail.attachments[0].data.startswith(b"%PDF")

    patched = client.patch(f"{base}/{s['id']}", json={"frequency": "weekly", "weekday": 0, "enabled": False})
    assert patched.status_code == 200 and patched.json()["enabled"] is False and patched.json()["next_send_at"] is None
    assert client.delete(f"{base}/{s['id']}").status_code == 200
    assert client.get(base).json() == []
    actions = [e["action"] for e in client.get(f"/api/orgs/{org['id']}/audit").json()]
    for a in ("report_schedule.created", "report.sent_now", "report_schedule.updated", "report_schedule.deleted"):
        assert a in actions


def test_due_reports_are_sent_once(app, client, client2):
    org, site, dev, members = schedule_setup(app, client, client2)
    history(dev)
    resp = client.post(f"/api/orgs/{org['id']}/report-schedules", json={
        "site_id": site["id"], "frequency": "daily", "hour": 6,
        "recipients": [members["owner@example.com"], members["boss@example.com"]]})
    sid = resp.json()["id"]
    now = utcnow()
    with session_factory()() as db:
        # a new schedule waits for its first send time
        assert reports.send_due(db, now)["sent"] == 0
        db.execute(update(ReportSchedule).values(created_at=now - timedelta(days=3)))
        db.commit()
        email.SENT.clear()
        result = reports.send_due(db, now)
        assert result == {"sent": 1, "emails": 2, "failed": 0}
        assert len(email.SENT) == 2 and email.SENT[0].attachments[0].filename.endswith(".pdf")
        # the same period is never sent again, even after a restart
        assert reports.send_due(db, now + timedelta(minutes=5))["sent"] == 0
        assert len(email.SENT) == 2
        row = db.get(ReportSchedule, sid)
        due = reports.latest_send("daily", 0, 6, "Europe/Berlin", now)
        assert row.last_period_end == due.last and row.last_error is None

    # the next day it goes out again; a person who left the organization gets nothing
    with session_factory()() as db:
        from countvision_cloud.saas.models import Membership
        db.execute(Membership.__table__.delete().where(Membership.org_id == org["id"],
                                                       Membership.role == "viewer"))
        db.commit()
        email.SENT.clear()
        assert reports.send_due(db, now + timedelta(days=1))["emails"] == 1
        assert [m.to for m in email.SENT] == ["owner@example.com"]


def test_report_errors_are_recorded_and_skipped(app, client, client2, monkeypatch):
    org, site, dev, members = schedule_setup(app, client, client2)
    client.post(f"/api/orgs/{org['id']}/report-schedules", json={
        "site_id": site["id"], "frequency": "daily", "recipients": [members["owner@example.com"]]})
    now = datetime.now(UTC)
    with session_factory()() as db:
        db.execute(update(ReportSchedule).values(created_at=now - timedelta(days=3)))
        db.commit()

        def boom(*a, **k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(reports.pdf, "build_pdf", boom)
        email.SENT.clear()
        for i in range(reports.MAX_FAILURES):
            assert reports.send_due(db, now + timedelta(minutes=5 * i))["failed"] == 1
        db.expire_all()
        row = db.scalars(select(ReportSchedule)).first()
        assert "disk full" in row.last_error and row.last_period_end is not None  # gave up on that period
        assert reports.send_due(db, now + timedelta(minutes=30))["failed"] == 0
        assert email.SENT == []


def test_sample_data_add_and_remove(app, client, client2):
    signup(client, "owner@example.com", "Olga")
    org = client.post("/api/orgs", json={"name": "Café Sonne"}).json()
    resp = client.post(f"/api/orgs/{org['id']}/sample-data")
    assert resp.status_code == 201, resp.text
    site_id = resp.json()["site_id"]
    assert client.post(f"/api/orgs/{org['id']}/sample-data").status_code == 409
    y = today() - timedelta(days=1)
    week = client.get(f"/api/orgs/{org['id']}/sites/{site_id}/dashboard",
                      params={"from": (y - timedelta(days=6)).isoformat(), "to": y.isoformat()}).json()
    assert week["has_data"] and week["totals"]["in"] > 300 and week["zones"][0]["kind"] == "queue"
    assert abs(week["totals"]["in"] - week["totals"]["out"]) < week["totals"]["in"] * 0.1  # people also leave
    assert week["dwell"]["count"] > 0 and week["classes"][0]["class_name"] == "person"
    # members cannot add or remove it, admins can
    signup(client2, "m@example.com", "Mia")
    add_member(org["id"], "m@example.com", Role.MEMBER)
    assert client2.delete(f"/api/orgs/{org['id']}/sample-data").status_code == 403
    assert client.delete(f"/api/orgs/{org['id']}/sample-data").status_code == 200
    assert client.get(f"/api/orgs/{org['id']}/sites").json() == []
    assert client.delete(f"/api/orgs/{org['id']}/sample-data").status_code == 404


def test_no_change_percent_when_the_comparison_period_has_little_data(app, client):
    """First weeks after installation: last week had only 1 of 4 analysed hours -> no "+300 %"."""
    org, site, code, dev, body = setup(client, app)
    y = today() - timedelta(days=1)
    lw = y - timedelta(days=7)
    dev.post("/api/device/ingest", json=batch("c", line_counts=[
        lc("door", local_ts(y, 10), "entrance", "*", 40), lc("door", local_ts(lw, 10), "entrance", "*", 10)],
        coverage=cov("door", local_ts(y, 9), 240) + cov("door", local_ts(lw, 10), 60)))
    d = dash(client, org, site, y)
    assert d["totals"]["compare_in"] == 10 and d["totals"]["change_pct"] is None and d["totals"]["comparable"] is False
    assert d["lines"][0]["change_pct"] is None
    # with the same running time both weeks the change is shown
    dev.post("/api/device/ingest", json=batch("d", coverage=cov("door", local_ts(lw, 9), 240)))
    assert dash(client, org, site, y)["totals"]["change_pct"] == 300.0


def test_report_mail_survives_the_worker_queue_and_smtp(monkeypatch):
    """Celery sends mails as JSON: attachments must survive that, and SMTP must attach them."""
    import json

    mail = email.build("a@example.com", "Report", "Report", ["Hello"])
    from dataclasses import replace

    mail = replace(mail, attachments=(email.Attachment.of("r.pdf", "application/pdf", b"%PDF-1.4 test"),))
    again = email.Mail.from_dict(json.loads(json.dumps(mail.as_dict())))
    assert again == mail and again.attachments[0].data == b"%PDF-1.4 test"

    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(email.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(email, "get_settings", lambda: type("S", (), {
        "email_backend": "smtp", "email_from": "x@example.com", "smtp_host": "h", "smtp_port": 25,
        "smtp_starttls": False, "smtp_user": None, "smtp_password": None})())
    email.deliver(again)
    parts = [p for p in sent[0].iter_attachments()]
    assert parts[0].get_filename() == "r.pdf" and parts[0].get_content() == b"%PDF-1.4 test"


def test_new_camera_is_expected_only_since_it_was_added(app, client):
    org, site, code, dev, body = setup(client, app)
    now = int(time.time())
    dev.post("/api/device/ingest", json=batch("new", coverage=cov("door", now - now % 60 - 180, 2)))
    d = dash(client, org, site, datetime.fromtimestamp(now - 180, BERLIN).date(), today())
    cam = d["coverage"]["cameras"][0]
    assert cam["seconds"] == 120 and cam["expected_s"] < 600 and cam["pct"] > 20  # not "0 % of the whole day"
