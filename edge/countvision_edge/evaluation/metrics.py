"""Metrics: compare counted events with hand labels.

Two views of line counting, because they catch different mistakes:

* **Count accuracy** = 1 - sum|counted - true| / sum(true), per line, direction and class.
  This is what a customer sees ("we had 120 visitors"). It is the number in the quality
  targets. Weakness: one missed person plus one double count cancel out.
* **Event F1**: every counted crossing must match a real crossing of the same line,
  direction and class within ``tolerance_s`` seconds. This catches errors that cancel out.

Occupancy: at each labeled time the counted occupancy is compared with the true one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field

Key = tuple[str, str, str]  # (line, direction, class)


@dataclass(frozen=True)
class Crossing:
    t: float
    line: str
    dir: str
    cls: str

    @property
    def key(self) -> Key:
        return (self.line, self.dir, self.cls)


def match_times(truth: Sequence[float], counted: Sequence[float], tolerance_s: float) -> int:
    """Maximum number of one-to-one pairs with |t_true - t_counted| <= tolerance.

    Both lists are sorted; the greedy two-pointer walk is optimal for this 1-D matching.
    """
    a, b = sorted(truth), sorted(counted)
    i = j = matched = 0
    while i < len(a) and j < len(b):
        if abs(a[i] - b[j]) <= tolerance_s:
            matched += 1
            i += 1
            j += 1
        elif a[i] < b[j]:
            i += 1
        else:
            j += 1
    return matched


@dataclass
class KeyRow:
    """Counts for one (line, direction, class)."""

    line: str
    dir: str
    cls: str
    true: int
    counted: int
    matched: int

    @property
    def abs_error(self) -> int:
        return abs(self.counted - self.true)


@dataclass
class CrossingMetrics:
    rows: list[KeyRow] = field(default_factory=list)

    @property
    def true_total(self) -> int:
        return sum(r.true for r in self.rows)

    @property
    def counted_total(self) -> int:
        return sum(r.counted for r in self.rows)

    @property
    def matched_total(self) -> int:
        return sum(r.matched for r in self.rows)

    @property
    def abs_error_total(self) -> int:
        return sum(r.abs_error for r in self.rows)

    def to_dict(self) -> dict:
        return {
            "rows": [asdict(r) for r in self.rows],
            "true": self.true_total,
            "counted": self.counted_total,
            "matched": self.matched_total,
            "abs_error": self.abs_error_total,
            "count_accuracy": count_accuracy(self.abs_error_total, self.true_total),
            "precision": ratio(self.matched_total, self.counted_total),
            "recall": ratio(self.matched_total, self.true_total),
            "f1": f1(self.matched_total, self.counted_total, self.true_total),
        }


def compare_crossings(
    truth: Iterable[Crossing], counted: Iterable[Crossing], tolerance_s: float = 2.0
) -> CrossingMetrics:
    """Per-key counts and timing matches. Keys with no events on both sides are omitted."""
    by_key_true: dict[Key, list[float]] = {}
    by_key_counted: dict[Key, list[float]] = {}
    for c in truth:
        by_key_true.setdefault(c.key, []).append(c.t)
    for c in counted:
        by_key_counted.setdefault(c.key, []).append(c.t)
    metrics = CrossingMetrics()
    for key in sorted(set(by_key_true) | set(by_key_counted)):
        t_true, t_counted = by_key_true.get(key, []), by_key_counted.get(key, [])
        metrics.rows.append(
            KeyRow(*key, true=len(t_true), counted=len(t_counted),
                   matched=match_times(t_true, t_counted, tolerance_s))
        )
    return metrics


@dataclass
class OccupancyMetrics:
    """``true_sum``/``counted_sum`` over all samples; their ratio is the error of the average."""

    samples: int = 0
    true_sum: int = 0
    counted_sum: int = 0
    abs_error_sum: int = 0
    exact: int = 0

    def add(self, true_n: int, counted_n: int) -> None:
        self.samples += 1
        self.true_sum += true_n
        self.counted_sum += counted_n
        self.abs_error_sum += abs(counted_n - true_n)
        self.exact += int(counted_n == true_n)

    def merge(self, other: OccupancyMetrics) -> None:
        self.samples += other.samples
        self.true_sum += other.true_sum
        self.counted_sum += other.counted_sum
        self.abs_error_sum += other.abs_error_sum
        self.exact += other.exact

    @classmethod
    def from_dict(cls, data: dict) -> OccupancyMetrics:
        return cls(
            samples=data["samples"],
            true_sum=data["true_sum"],
            counted_sum=data["counted_sum"],
            abs_error_sum=data["abs_error_sum"],
            exact=data["exact"],
        )

    def to_dict(self) -> dict:
        return {
            "samples": self.samples,
            "true_sum": self.true_sum,
            "counted_sum": self.counted_sum,
            "abs_error_sum": self.abs_error_sum,
            "exact": self.exact,
            "mae": ratio(self.abs_error_sum, self.samples),
            "mean_error": ratio(abs(self.counted_sum - self.true_sum), self.true_sum),
            "sample_error": ratio(self.abs_error_sum, self.true_sum),
            "exact_rate": ratio(self.exact, self.samples),
        }


def value_at(series: Sequence[tuple[float, int]], t: float) -> int:
    """Value of a step series [(time, value), ...] (sorted) at time ``t``: the last sample at or
    before ``t``; before the first sample, the first value; empty series -> 0."""
    if not series:
        return 0
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= t + 1e-9:
            lo = mid + 1
        else:
            hi = mid
    return series[max(lo - 1, 0)][1]


def count_accuracy(abs_error: int, true_total: int) -> float | None:
    """1 - error / true, never below 0. None when there was nothing to count."""
    if true_total <= 0:
        return None
    return max(0.0, 1.0 - abs_error / true_total)


def ratio(num: float, den: float) -> float | None:
    return None if den <= 0 else num / den


def f1(matched: int, counted: int, true: int) -> float | None:
    if counted + true == 0:
        return None
    return 2 * matched / (counted + true)
