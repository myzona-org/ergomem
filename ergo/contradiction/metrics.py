"""In-process Prometheus-text metrics registry (ergo's private-repo issue #121).

Zero extra deps -- stdlib only, matching house style (stdlib-first,
no heavy dependency for a hand-rollable feature). Counters/histograms live as
plain module-level globals, guarded by one lock: the server is single-process
by design (SQLite is single-writer, ERGO_HTTP_MAX_WORKERS bounds the thread
pool), so there is no cross-process aggregation to do
and RESET-ON-RESTART IS ACCEPTABLE AND EXPECTED -- see docs/HTTP-API.md's
`/metrics` section and docs/CONFIGURATION.md.

Four families are exposed, matching ergo's private-repo issue #121's ask:
  - `ergo_http_requests_total{method,route,status}`            (Counter)
  - `ergo_http_request_duration_seconds{method,route}`         (Histogram)
  - `ergo_guarded_write_outcomes_total{route,outcome}`         (Counter)
  - `ergo_pipeline_stage_duration_seconds{stage}`               (Histogram)
  - `ergo_http_workers{name="active"|"max"}`                   (Gauge, via
    `render()`'s `gauges` argument -- server.py reads them straight off the
    live `BoundedThreadingHTTPServer` instance, this module has no reference
    to it)

`reset()` exists for test isolation only (each metrics test starts from a
clean registry); production code never calls it.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

# RLock, not Lock: `render()` (below) takes a snapshot of all four metric
# families under ONE continuous hold of `_LOCK` for scrape-consistency (PR-
# Agent finding: four independently-locked `.snapshot()` calls back-to-back
# let a concurrent writer land between them, producing an exposition where
# a histogram's bucket/sum/count don't correspond to the same instant) --
# that means each family's own `.snapshot()` call below re-enters `_LOCK`
# from the SAME thread that's already holding it, which only a re-entrant
# lock permits without deadlocking.
_LOCK = threading.RLock()

# Seconds. Covers everything from a sub-millisecond DB read up to a slow
# multi-second LLM normalizer round trip.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)


class Counter:
    """A label-keyed monotonic counter. `labels` is any hashable tuple."""

    def __init__(self) -> None:
        self._values: dict[tuple, int] = {}

    def inc(self, labels: tuple, amount: int = 1) -> None:
        with _LOCK:
            self._values[labels] = self._values.get(labels, 0) + amount

    def snapshot(self) -> dict[tuple, int]:
        with _LOCK:
            return dict(self._values)

    def reset(self) -> None:
        with _LOCK:
            self._values.clear()


class Histogram:
    """A label-keyed cumulative histogram (Prometheus bucket semantics: each
    bucket count is the number of observations <= its boundary, plus a
    trailing +Inf bucket equal to the total observation count)."""

    def __init__(self, buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        self.buckets = buckets
        self._counts: dict[tuple, list[int]] = {}
        self._sums: dict[tuple, float] = {}
        self._totals: dict[tuple, int] = {}

    def observe(self, labels: tuple, value: float) -> None:
        with _LOCK:
            counts = self._counts.setdefault(labels, [0] * (len(self.buckets) + 1))
            for i, boundary in enumerate(self.buckets):
                if value <= boundary:
                    counts[i] += 1
            counts[-1] += 1  # +Inf
            self._sums[labels] = self._sums.get(labels, 0.0) + value
            self._totals[labels] = self._totals.get(labels, 0) + 1

    def snapshot(self) -> dict:
        with _LOCK:
            return {
                "counts": {k: list(v) for k, v in self._counts.items()},
                "sums": dict(self._sums),
                "totals": dict(self._totals),
            }

    def reset(self) -> None:
        with _LOCK:
            self._counts.clear()
            self._sums.clear()
            self._totals.clear()


# --- the four registered metric families ---------------------------------
REQUEST_COUNT = Counter()          # (method, route, status) -> count
REQUEST_DURATION = Histogram()     # (method, route)         -> latency
WRITE_OUTCOME = Counter()          # (route, outcome)        -> count
STAGE_DURATION = Histogram()       # (stage,)                -> latency


def reset() -> None:
    """Clear every metric. Test-only -- see module docstring."""
    REQUEST_COUNT.reset()
    REQUEST_DURATION.reset()
    WRITE_OUTCOME.reset()
    STAGE_DURATION.reset()


@contextmanager
def time_stage(stage: str):
    """Context manager: record `stage`'s wall-clock duration into
    STAGE_DURATION regardless of whether the block raises."""
    start = time.monotonic()
    try:
        yield
    finally:
        STAGE_DURATION.observe((stage,), time.monotonic() - start)


def _fmt(value) -> str:
    # str() and repr() are IDENTICAL for floats in Python 3 (both give the
    # shortest decimal string that round-trips to the exact same float --
    # never lossy, unlike Python 2's repr()) -- str() is used here simply
    # because it's the conventional choice for serialization, not because
    # repr() would have behaved any differently.
    return str(value)


def _escape(label_value: str) -> str:
    return label_value.replace("\\", "\\\\").replace('"', '\\"')


def render(gauges: dict[str, float] | None = None) -> str:
    """Render every metric as Prometheus text exposition format (text/plain;
    version=0.0.4). `gauges` (optional) are simple name->value pairs appended
    as individual gauge metrics -- used by server.py for the thread-pool
    saturation gauges, which live on the running server instance, not here.

    All four families' snapshots are taken under ONE continuous hold of
    `_LOCK` (see its docstring) so a concurrent writer can never land
    between two of them -- every number in a single render() call reflects
    the exact same instant, matching Prometheus exposition semantics.
    String formatting (the loops below) happens AFTER the lock is released:
    only the raw dict reads need it.
    """
    with _LOCK:
        request_count = REQUEST_COUNT.snapshot()
        request_duration = REQUEST_DURATION.snapshot()
        write_outcome = WRITE_OUTCOME.snapshot()
        stage_duration = STAGE_DURATION.snapshot()

    lines: list[str] = []

    lines.append("# HELP ergo_http_requests_total Total HTTP requests, by method/route/status.")
    lines.append("# TYPE ergo_http_requests_total counter")
    for (method, route, status), v in sorted(request_count.items()):
        lines.append(
            f'ergo_http_requests_total{{method="{_escape(method)}",'
            f'route="{_escape(route)}",status="{_escape(status)}"}} {v}')

    lines.append("# HELP ergo_http_request_duration_seconds HTTP request latency, by method/route.")
    lines.append("# TYPE ergo_http_request_duration_seconds histogram")
    for labels, counts in sorted(request_duration["counts"].items()):
        method, route = labels
        base = f'method="{_escape(method)}",route="{_escape(route)}"'
        for i, boundary in enumerate(REQUEST_DURATION.buckets):
            lines.append(
                f'ergo_http_request_duration_seconds_bucket{{{base},le="{_fmt(boundary)}"}} {counts[i]}')
        lines.append(
            f'ergo_http_request_duration_seconds_bucket{{{base},le="+Inf"}} {counts[-1]}')
        lines.append(
            f'ergo_http_request_duration_seconds_sum{{{base}}} {_fmt(request_duration["sums"][labels])}')
        lines.append(
            f'ergo_http_request_duration_seconds_count{{{base}}} {request_duration["totals"][labels]}')

    lines.append("# HELP ergo_guarded_write_outcomes_total Guarded-write (/remember,/learn,/supersede) outcomes, by route/outcome.")
    lines.append("# TYPE ergo_guarded_write_outcomes_total counter")
    for (route, outcome), v in sorted(write_outcome.items()):
        lines.append(
            f'ergo_guarded_write_outcomes_total{{route="{_escape(route)}",'
            f'outcome="{_escape(outcome)}"}} {v}')

    lines.append("# HELP ergo_pipeline_stage_duration_seconds Contradiction-pipeline stage latency (normalize/nli/judge).")
    lines.append("# TYPE ergo_pipeline_stage_duration_seconds histogram")
    for labels, counts in sorted(stage_duration["counts"].items()):
        (stage,) = labels
        base = f'stage="{_escape(stage)}"'
        for i, boundary in enumerate(STAGE_DURATION.buckets):
            lines.append(
                f'ergo_pipeline_stage_duration_seconds_bucket{{{base},le="{_fmt(boundary)}"}} {counts[i]}')
        lines.append(
            f'ergo_pipeline_stage_duration_seconds_bucket{{{base},le="+Inf"}} {counts[-1]}')
        lines.append(
            f'ergo_pipeline_stage_duration_seconds_sum{{{base}}} {_fmt(stage_duration["sums"][labels])}')
        lines.append(
            f'ergo_pipeline_stage_duration_seconds_count{{{base}}} {stage_duration["totals"][labels]}')

    # `is not None`, NEVER plain truthiness -- an explicitly-passed empty
    # dict is a legitimate "no gauges this call" value, distinct from the
    # None default meaning "not supplied" -- an easy truthiness trap to
    # fall into (`if _REGISTRY` vs `is not None`).
    if gauges is not None:
        lines.append("# HELP ergo_http_workers Thread-pool worker gauges (name=\"active\"|\"max\").")
        lines.append("# TYPE ergo_http_workers gauge")
        for name, value in sorted(gauges.items()):
            lines.append(f'ergo_http_workers{{name="{_escape(name)}"}} {_fmt(value)}')

    return "\n".join(lines) + "\n"
