"""Single-tenant provider — stands in for the private `.control` /
`.tenant_registry` modules that the SaaS control plane needs and a
single-tenant deployment does not.

The generated public-core tree replaces `from .control import (...)` /
`from .tenant_registry import TenantRegistry` in server.py with an import
from this module, and boot() constructs no control plane at all
(`_CONTROL = None`, `_REGISTRY = None`). Every non-admin route already
branches on `principal.tenant_id is None` before it ever touches `_CONTROL`/`_REGISTRY`
(LEGACY_PRINCIPAL — the only principal a single-tenant deployment ever
produces — always has `tenant_id=None`), so the classes below exist purely
to satisfy the `ControlStore | None` / `TenantRegistry | None` type
annotations on the module globals. Neither is ever instantiated.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    org_id: str | None
    tenant_id: str | None
    who: str | None
    role: str
    rate_limit_per_min_override: int | None = None
    max_active_claims_override: int | None = None
    rate_limit_burst_per_min_override: int | None = None
    plan: str | None = None
    default_project: str | None = None


# Same back-compat identity as the private control.py: a caller presenting
# the legacy ERGO_API_TOKEN (or no token at all in open dev/test mode) is
# this principal — org_id/project/who come from the request body, routed to
# the single pooled store. tenant_id is None, which is what exempts it from
# every control-plane-gated code path below it in server.py (rate limiting,
# monthly write metering, the tenant-registry store lease).
LEGACY_PRINCIPAL = Principal(org_id=None, tenant_id=None, who=None, role="legacy")


def plan_limits(plan: str | None) -> dict | None:
    """A single-tenant deployment has no plans — always unlimited."""
    return None


def current_month_key(now: float | None = None) -> str:
    return ""


def month_reset_at(month_key: str) -> str:
    return ""


class ControlStore:
    """Type-only stand-in — never constructed (boot() sets `_CONTROL =
    None`); exists so `_CONTROL: ControlStore | None` type-checks."""


class TenantRegistry:
    """Type-only stand-in — never constructed (boot() sets `_REGISTRY =
    None`); exists so `_REGISTRY: TenantRegistry | None` type-checks."""
