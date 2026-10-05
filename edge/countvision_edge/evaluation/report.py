"""Turn evaluation and benchmark JSON files into Markdown tables for ``eval/results.md``.

Only the part between the two AUTO markers is rewritten, so hand-written text around it
(conclusions, recommendation) stays.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .labels import ClipLabels

START = "<!-- AUTO:START (countvision-edge eval report rewrites this part) -->"
END = "<!-- AUTO:END -->"


def pct(value: float | None, digits: int = 1) -> str:
    return "–" if value is None else f"{value * 100:.{digits}f} %"


def num(value: float | None, digits: int = 1) -> str:
    return "–" if value is None else f"{value:.{digits}f}"


def _table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(out)


def load_json_dir(folder: str | Path, kind: str) -> list[dict]:
    root = Path(folder)
    if not root.is_dir():
        return []
    items = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("kind") == kind:
            data["_file"] = path.name
            items.append(data)
    return items


def clips_table(clips: list[ClipLabels], durations: dict[str, float] | None = None) -> str:
    rows = []
    for c in sorted(clips, key=lambda c: ({"tune": 0, "test": 1}.get(c.split, 2), c.clip)):
        n_in = sum(1 for x in c.crossings if x.dir == "in")
        n_out = len(c.crossings) - n_in
        dur = (durations or {}).get(c.clip)
        rows.append([
            f"`{c.clip}`", c.split, c.scene, num(dur, 0) + " s" if dur else "–",
            ", ".join(c.classes), f"{len(c.crossings)} ({n_in} in / {n_out} out)",
            str(len(c.occupancy)), ", ".join(c.tags) or "–",
            "yes" if c.verified_by else "no",
        ])
    return _table(["Clip", "Split", "Scene", "Length", "Classes", "True crossings",
                   "Occupancy samples", "Tags", "Verified"], rows)


def accuracy_table(runs: list[dict]) -> str:
    rows = []
    for run in runs:
        s = run["summary"]
        scene = s.get("by_scene", {})
        rows.append([
            run.get("name", run["_file"]), f"`{run['detector']}`", run.get("tag") or "–", run["split"],
            pct(s["all"]["crossings"]["count_accuracy"]),
            pct(scene.get("clear", {}).get("crossings", {}).get("count_accuracy")),
            pct(scene.get("hard", {}).get("crossings", {}).get("count_accuracy")),
            pct(s["all"]["crossings"]["f1"]),
            f"{s['all']['crossings']['counted']} / {s['all']['crossings']['true']}",
            pct(s["all"]["occupancy"]["mean_error"]),
            num(s["all"]["occupancy"]["mae"], 2),
        ])
    return _table(["Run", "Detector", "Params", "Split", "Count acc.", "Clear", "Hard", "Event F1",
                   "Counted / true", "Occ. error (avg)", "Occ. MAE"], rows)


def before_after_table(runs: list[dict]) -> str:
    """One row per detector and split that has both a 'before' and an 'after' run."""
    groups: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for run in runs:
        if run.get("tag") in ("before", "after"):
            groups[(run["detector"], run["split"])][run["tag"]] = run
    rows = []
    for (detector, split), pair in sorted(groups.items()):
        if "before" not in pair or "after" not in pair:
            continue
        b, a = pair["before"]["summary"]["all"], pair["after"]["summary"]["all"]
        rows.append([
            f"`{detector}`", split,
            f"{pct(b['crossings']['count_accuracy'])} → **{pct(a['crossings']['count_accuracy'])}**",
            f"{pct(b['crossings']['f1'])} → {pct(a['crossings']['f1'])}",
            f"{pct(b['occupancy']['mean_error'])} → {pct(a['occupancy']['mean_error'])}",
            f"{num(b['occupancy']['mae'], 2)} → {num(a['occupancy']['mae'], 2)}",
        ])
    if not rows:
        return "_No before/after pair yet (run `eval run --tag before` and `--tag after`)._"
    return _table(["Detector", "Split", "Count accuracy", "Event F1", "Occupancy error (avg)",
                   "Occupancy MAE"], rows)


def per_clip_table(run: dict) -> str:
    rows = []
    for clip in run["clips"]:
        c, o = clip["crossings"], clip["occupancy"]
        rows.append([
            f"`{clip['clip']}`", clip["scene"], f"{c['counted']} / {c['true']}",
            pct(c["count_accuracy"]), pct(c["f1"]),
            f"{o['counted_sum']} / {o['true_sum']}" if o["samples"] else "–",
            pct(o["mean_error"]), num(o["mae"], 2),
        ])
    return _table(["Clip", "Scene", "Counted / true", "Count acc.", "Event F1",
                   "Occ. counted / true", "Occ. error (avg)", "Occ. MAE"], rows)


def bench_table(bench: dict) -> str:
    rows = []
    for r in bench["results"]:
        if r.get("error"):
            rows.append([f"`{r['spec']}`", _short_license(r["license"]), "–", "–", "–", "–", "–", "–",
                         "error: " + r["error"][:80].replace("|", "/")])
            continue
        rows.append([
            f"`{r['spec']}`", _short_license(r["license"]), f"{r['runtime']} ({r['device']})",
            str(r["imgsz"] or "own"), f"{num(r['det_mean_ms'])} / {num(r['det_p95_ms'])}",
            num(r["det_fps"]), num(r["pipe_fps"]),
            f"{r['cameras_at_10fps']} / {r['cameras_at_15fps']}", "",
        ])
    m = bench["machine"]
    size = next((r.get("frame_size") for r in bench["results"] if r.get("frame_size")), "?")
    head = (f"**{bench['label']}** — {m['cpu']}, {m['cores_physical']} cores / {m['cores_logical']} "
            f"threads, {m['ram_gb']} GB RAM, GPU: {m['gpu'] or 'none'}, {m['os']}. "
            f"{bench['frames']} frames of `{bench['video']}` resized to {size}. "
            f"Date: {bench['created'][:10]}.")
    return head + "\n\n" + _table(
        ["Model", "Licence", "Runtime", "Input", "Detector ms (mean / p95)", "Detector FPS",
         "Pipeline FPS", "Cameras @10 / @15 FPS", "Note"], rows)


def _short_license(text: str) -> str:
    return text.split(" (")[0] if text else "?"


def tune_section(tunes: list[dict]) -> str:
    parts = []
    for t in tunes:
        best = t["trials"][0]
        default = next((x for x in t["trials"] if x["changed"] == 0), None)
        s, d = best["summary"]["crossings"], (default or {}).get("summary", {}).get("crossings", {})
        changed = {k: v for k, v in t["best"].items() if v != t.get("defaults", {}).get(k, v)}
        parts.append(
            f"- `{t['detector']}` on {', '.join(t['clips'])}: {t.get('n_trials', len(t['trials']))} "
            f"combinations in "
            f"{t['seconds']} s. Default params: {pct(d.get('count_accuracy'))} count accuracy, "
            f"best: {pct(s['count_accuracy'])} (F1 {pct(s['f1'])}). Changed: "
            + (", ".join(f"`{k}={v}`" for k, v in changed.items()) or "nothing (defaults were best)")
        )
    return "\n".join(parts) or "_No tuning run yet._"


def build_auto_block(clips: list[ClipLabels], runs: list[dict], tunes: list[dict], benches: list[dict],
                     durations: dict[str, float] | None = None) -> str:
    parts = [START, "", "### Clips", "", clips_table(clips, durations), "",
             "### Tuning (tune clips only)", "", tune_section(tunes), "",
             "### Before → after", "",
             "_before_ = Phase 1 code with default params. _after_ = current code with the params "
             "tuned on the tune clips. Read the test rows: the test clips were never used for tuning.",
             "", before_after_table(runs), "",
             "### All accuracy runs", "", accuracy_table(runs) if runs else "_No runs yet._", ""]
    for run in runs:
        if (run.get("tag") == "after" and run["split"] == "test") or run.get("detail"):
            parts += [f"#### Per clip: {run.get('name', run['_file'])} (`{run['detector']}`)", "",
                      per_clip_table(run), ""]
    parts += ["### Speed", ""]
    for bench in benches:
        parts += [bench_table(bench), ""]
    if not benches:
        parts += ["_No benchmark yet._", ""]
    parts.append(END)
    return "\n".join(parts)


def write_results(path: str | Path, block: str, title: str = "# CountVision evaluation results") -> Path:
    """Replace the AUTO block in ``path`` (create the file if needed)."""
    out = Path(path)
    if out.exists():
        text = out.read_text(encoding="utf-8")
        if START in text and END in text:
            before = text.split(START)[0]
            after = text.split(END, 1)[1]
            text = before + block + after
        else:
            text = text.rstrip() + "\n\n" + block + "\n"
    else:
        text = f"{title}\n\n{block}\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out
