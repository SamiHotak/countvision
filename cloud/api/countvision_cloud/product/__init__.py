"""The CountVision product on top of the SaaS basics: sites, devices (pairing, ingest), cameras."""

from .services import admin_facts as _admin_facts  # noqa: F401  (devices etc. on the admin page)
from .services import plan_limits as _plan_limits  # noqa: F401  (registers plan usage with billing)
