"""Smoke test of an INSTALLED CountVision Local (GitHub Actions, windows-latest, after setup.exe).

It talks to the app exactly like the CountVision window does (http://127.0.0.1:3000, the /api proxy
of the bundled web server), with the Python standard library only:

    python smoke_test.py wait                        until the app answers (after CountVision.exe started)
    python smoke_test.py fresh --state s.json --mjpeg URL --video demo.avi
        setup wizard (owner, business, site) -> log out -> log in -> licence trial -> camera (MJPEG
        test stream) counting with the bundled YOLOX model -> live picture (JPEG, blurred) -> video
        analysis job on the demo video -> PDF report -> backup (pg_dump.exe)
    python smoke_test.py after-update --state s.json
        the same data after installing again over it: log in with the same password, the automatic
        "before-update" backup exists, the camera counts again

Exit code 0 = all checks passed. Every step prints one line.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE = "http://127.0.0.1:3000"
EMAIL = "owner@example.com"
PASSWORD = "Smoke-test-password-42"
LINES = {"lines": [{"name": "door", "p1": [0.1, 0.5], "p2": [0.9, 0.5], "in_direction": "to_right"}],
         "zones": [], "classes": ["person", "car"], "anchor": "bottom_center", "sample_fps": None, "blur": True}


class Failed(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failed(message)


class Client:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method: str, path: str, body: Any = None, *, raw: bytes | None = None,
             timeout: float = 60) -> tuple[int, dict[str, str], bytes]:
        headers = {"X-CountVision": "1"}
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif raw is not None:
            headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def json(self, method: str, path: str, body: Any = None, *, expect: int | tuple[int, ...] = 200, **kw) -> Any:
        status, _, data = self.call(method, path, body, **kw)
        wanted = (expect,) if isinstance(expect, int) else expect
        check(status in wanted, f"{method} {path} -> {status} (wanted {wanted}): {data[:400]!r}")
        return json.loads(data) if data else None


def step(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def wait_until(what: str, probe, timeout: float, every: float = 2.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = probe()
        except (OSError, Failed, ValueError) as exc:
            last = exc
        else:
            if last:
                return last
        time.sleep(every)
    raise Failed(f"{what}: not within {timeout:.0f} s (last: {last!r})")


# -- steps ------------------------------------------------------------------------------------------


def wait_ready(timeout: float) -> dict:
    client = Client()

    def probe():
        status, _, data = client.call("GET", "/api/edition", timeout=5)
        return json.loads(data) if status == 200 else None

    edition = wait_until("the app answers on port 3000", probe, timeout)
    check(edition["edition"] == "local", f"not the local edition: {edition}")
    step(f"app answers: CountVision Local {edition.get('version')}, needs_setup={edition.get('needs_setup')}")
    return edition


def camera_state(client: Client, org: str, device: str) -> dict | None:
    cams = client.json("GET", f"/api/orgs/{org}/devices/{device}")["cameras"]
    return cams[0] if cams and cams[0].get("state") == "running" else None


def fresh(args) -> None:
    edition = wait_ready(args.timeout)
    check(edition["needs_setup"] is True and edition.get("live_picture") is True, f"unexpected edition {edition}")
    c = Client()
    out = c.json("POST", "/api/local/setup", {"name": "Smoke Test", "email": EMAIL, "password": PASSWORD,
                                              "business": "Smoke Test Café", "site_name": "Main street",
                                              "timezone": "Europe/Berlin"}, expect=201)
    org, site, device = out["org_id"], out["site_id"], out["device_id"]
    step("setup wizard: owner, business and site created")

    c.json("POST", "/api/auth/logout")
    check(c.call("GET", "/api/me")[0] == 401, "still logged in after logout")
    c.json("POST", "/api/auth/login", {"email": EMAIL, "password": PASSWORD})
    check(c.json("GET", "/api/me")["email"] == EMAIL, "login did not work")
    step("log out + log in: ok")

    licence = c.json("GET", f"/api/orgs/{org}/licence")
    check(licence["state"] == "trial", f"licence state {licence['state']}")
    step(f"licence: trial, {licence.get('trial_days_left')} days")

    c.json("POST", f"/api/orgs/{org}/devices/{device}/cameras",
           {"name": "Test camera", "kind": "http", "address": args.mjpeg}, expect=201)
    cam = wait_until("the camera counts (bundled YOLOX model)", lambda: camera_state(c, org, device), 240)
    detail = c.json("GET", f"/api/orgs/{org}/cameras/{cam['id']}")
    step(f"camera counting: state {detail['state']}, {detail.get('fps')} FPS")

    def live_picture():
        status, headers, data = c.call("GET", f"/api/orgs/{org}/cameras/{cam['id']}/live.jpg", timeout=20)
        return (headers, data) if status == 200 and data[:2] == b"\xff\xd8" else None

    headers, data = wait_until("live picture", live_picture, 60)
    mode = {k.lower(): v for k, v in headers.items()}.get("x-live-mode")
    check(mode == "blur", f"live picture mode {mode} (default must be people blurred)")
    step(f"live picture: JPEG {len(data) // 1024} kB, mode {mode}")

    video_job(c, org, Path(args.video))

    status, headers, pdf = c.call("GET", f"/api/orgs/{org}/sites/{site}/report.pdf")
    check(status == 200 and pdf[:4] == b"%PDF", f"report.pdf -> {status}")
    step(f"PDF report: {len(pdf) // 1024} kB")

    backup = c.json("POST", f"/api/orgs/{org}/backups", {}, expect=201, timeout=300)
    step(f"backup (pg_dump): {backup.get('name')}")

    Path(args.state).write_text(json.dumps({"org": org, "site": site, "device": device}), encoding="utf-8")
    step("fresh installation: ALL CHECKS PASSED")


def video_job(c: Client, org: str, video: Path) -> None:
    data = video.read_bytes()
    base = f"/api/orgs/{org}/video-jobs"
    job = c.json("POST", base, {"name": video.name, "size_bytes": len(data)}, expect=(200, 201))
    c.json("PUT", f"{base}/{job['id']}/upload?offset=0", raw=data)
    c.json("POST", f"{base}/{job['id']}/complete")
    wait_until("video probed", lambda: c.json("GET", f"{base}/{job['id']}")["status"] == "ready", 120)
    c.json("PUT", f"{base}/{job['id']}/config", LINES)
    c.json("POST", f"{base}/{job['id']}/start", expect=(200, 202))

    def finished():
        now = c.json("GET", f"{base}/{job['id']}")
        if now["status"] in ("failed", "cancelled"):
            raise Failed(f"video job {now['status']}: {now.get('error')}")
        return now if now["status"] == "done" else None

    done = wait_until("video analysis finished", finished, 600, every=3)
    result, files = done["result"], done["files"]
    door = result["lines"][0]
    check(bool(files.get("report.pdf")), f"no PDF for the video job: {files}")
    codec = result.get("video_codec")
    if files.get("annotated.mp4"):
        status, _, mp4 = c.call("GET", f"{base}/{job['id']}/files/annotated.mp4")
        check(status == 200 and mp4[4:8] == b"ftyp", "annotated.mp4 is not an MP4 file")
    step(f"video analysis: {result.get('frames_processed')} frames, line IN {door['in']} / OUT {door['out']} "
         f"(YOLOX on synthetic shapes: counts are not checked), annotated video codec {codec}")


def after_update(args) -> None:
    edition = wait_ready(args.timeout)
    check(edition["needs_setup"] is False, "the data is gone: setup is asked again")
    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    c = Client()
    c.json("POST", "/api/auth/login", {"email": EMAIL, "password": PASSWORD})
    step("after the update: log in with the same password: ok")
    backups = c.json("GET", f"/api/orgs/{state['org']}/backups")["backups"]
    kinds = [b.get("kind") for b in backups]
    check("before-update" in kinds, f"no automatic backup before the update: {kinds}")
    step(f"backups: {kinds}")
    cam = wait_until("the camera counts again", lambda: camera_state(c, state["org"], state["device"]), 240)
    step(f"camera counting again: state {cam['state']}")
    step("update: ALL CHECKS PASSED")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["wait", "fresh", "after-update"])
    parser.add_argument("--state", default="smoke-state.json")
    parser.add_argument("--mjpeg", default="http://127.0.0.1:8090/video")
    parser.add_argument("--video", default="demo_scene.avi")
    parser.add_argument("--timeout", type=float, default=300, help="seconds to wait for the app")
    args = parser.parse_args(argv)
    try:
        if args.command == "wait":
            wait_ready(args.timeout)
        elif args.command == "fresh":
            fresh(args)
        else:
            after_update(args)
    except Failed as exc:
        print(f"FAILED: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
