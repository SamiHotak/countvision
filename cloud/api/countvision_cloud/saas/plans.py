"""Plans and their limits. The one place to change prices and limits.

The limit names ("cameras", "video_minutes") belong to CountVision; another product reuses the
billing code and only edits this file.

Prices are in euro per month WITHOUT VAT (B2B). Stripe prices are created from this list by
``countvision-cloud stripe-setup`` (lookup key ``countvision_<plan>_monthly``). If you change a
price here, run stripe-setup again: it makes a new Stripe price and moves the lookup key to it
(existing subscriptions keep their old price until they change plan).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    price_eur: int | None  # per month; None = not sold (trial, unlimited)
    limits: dict[str, int] = field(default_factory=dict)  # -1 = no limit
    blurb: str = ""

    @property
    def lookup_key(self) -> str:
        return f"countvision_{self.id}_monthly"

    def limit(self, name: str) -> int:
        return self.limits.get(name, -1)


# Sold plans, smallest first (the web page shows them in this order).
PLANS: dict[str, Plan] = {
    p.id: p
    for p in (
        Plan(
            "starter",
            "Starter",
            39,
            {"cameras": 1, "video_minutes": 60},
            "One entrance. For a small shop or café.",
        ),
        Plan(
            "shop",
            "Shop",
            119,
            {"cameras": 4, "video_minutes": 300},
            "Entrance, till queue and more. About 30 EUR per camera.",
        ),
        Plan(
            "business",
            "Business",
            299,
            {"cameras": 12, "video_minutes": 1200},
            "Several entrances or sites. About 25 EUR per camera.",
        ),
    )
}

# Free trial for every new organization (no card needed). Length: CV_TRIAL_DAYS (default 14).
TRIAL = Plan("trial", "Free trial", None, {"cameras": 2, "video_minutes": 60})

# Billing switched off (no Stripe key): self-hosted or development, no limits.
UNLIMITED = Plan("unlimited", "No limits (billing off)", None, {})

# Trial ended or subscription cancelled: nothing new can be added. Data already sent is kept.
NONE = Plan("none", "No active plan", None, {"cameras": 0, "video_minutes": 0})

LIMIT_LABELS = {"cameras": "cameras", "video_minutes": "minutes of video analysis per month"}


def plan_by_lookup_key(key: str | None) -> Plan | None:
    return next((p for p in PLANS.values() if p.lookup_key == key), None)


def get_plan(plan_id: str | None) -> Plan | None:
    return PLANS.get(plan_id or "")
