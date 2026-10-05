"""Evaluate many clips, and tune parameters on the TUNE split only.

Rule: never tune on the test set. ``grid_search`` refuses clips whose split is not "tune",
so the test numbers stay an honest estimate of how the product does on new scenes.
"""

from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Callable
from dataclasses import replace

from .runner import ClipResult, ClipRunner, EvalParams, aggregate, score

log = logging.getLogger(__name__)

DEFAULT_GRID: dict[str, list] = {
    "conf": [0.1, 0.2, 0.3, 0.4],
    "track_activation_threshold": [0.25, 0.4, 0.5, 0.6],
    "min_track_frames": [2, 3, 5],
    "lost_track_buffer": [15, 30, 60],
    "deadband_px": [2.0, 4.0, 8.0],
}


class SplitError(ValueError):
    """Raised when tuning is asked to look at test (or dev) clips."""


def evaluate(
    runners: list[ClipRunner], params: EvalParams, tolerance_s: float = 2.0, keep_events: bool = False
) -> tuple[list[ClipResult], dict]:
    """Run every clip with the same parameters. Returns per-clip results and summaries:
    {"all": ..., "by_scene": {...}, "by_split": {...}}."""
    results = [r.run(params, tolerance_s, keep_events) for r in runners]
    return results, summarize(results)


def summarize(results: list[ClipResult]) -> dict:
    by_scene = {
        scene: aggregate([r for r in results if r.scene == scene])
        for scene in sorted({r.scene for r in results})
    }
    by_split = {
        split: aggregate([r for r in results if r.split == split])
        for split in sorted({r.split for r in results})
    }
    return {"all": aggregate(results), "by_scene": by_scene, "by_split": by_split}


def expand_grid(grid: dict[str, list], base: EvalParams) -> list[EvalParams]:
    """All combinations of the grid values, applied on top of ``base``."""
    unknown = set(grid) - set(EvalParams.__dataclass_fields__)
    if unknown:
        raise ValueError(f"Unknown grid parameter(s): {', '.join(sorted(unknown))}")
    keys = list(grid)
    combos = itertools.product(*(grid[k] for k in keys))
    return [replace(base, **dict(zip(keys, values, strict=True))) for values in combos]


def grid_search(
    runners: list[ClipRunner],
    grid: dict[str, list],
    base: EvalParams | None = None,
    tolerance_s: float = 2.0,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Try every combination on the tune clips. Returns the trials sorted best first.

    Ties are broken by (count accuracy, event F1, low occupancy error) and then by staying
    close to the defaults (fewer changed parameters), which is the safer choice.
    """
    bad = [r.labels.clip for r in runners if r.labels.split != "tune"]
    if bad:
        raise SplitError(
            f"Tuning may only use clips with split: tune. Not allowed: {', '.join(bad)}"
        )
    if not runners:
        raise SplitError("No tune clips found. Mark 1-2 clips with split: tune.")
    base = base or EvalParams()
    candidates = expand_grid(grid, base)
    trials = []
    started = time.perf_counter()
    for i, params in enumerate(candidates, 1):
        _, summary = evaluate(runners, params, tolerance_s)
        changed = sum(
            1 for k, v in params.to_dict().items() if v != getattr(EvalParams(), k)
        )
        trials.append({"params": params.to_dict(), "summary": summary["all"],
                       "score": list(score(summary["all"])), "changed": changed})
        if progress:
            progress(i, len(candidates))
    trials.sort(key=lambda t: (tuple(t["score"]), -t["changed"]), reverse=True)
    return {
        "clips": [r.labels.clip for r in runners],
        "grid": grid,
        "n_trials": len(trials),
        "trials": keep_top(trials),
        "best": trials[0]["params"],
        "seconds": round(time.perf_counter() - started, 1),
    }


def keep_top(trials: list[dict], n: int = 40) -> list[dict]:
    """The best ``n`` trials plus the default-params trial (needed for "default vs best")."""
    top = trials[:n]
    default = next((t for t in trials if t["changed"] == 0), None)
    if default is not None and default not in top:
        top.append(default)
    return top
