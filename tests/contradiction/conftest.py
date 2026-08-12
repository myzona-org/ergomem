"""Shared test support for tests/contradiction/'s `start_server`/`serve`
fixtures.

Real incident, not hypothetical: this "boot the HTTP server on a background
thread, wait for it to come up" mechanic was copy-pasted across 12 files with
a hard `ready.wait(timeout=5)` budget and no way to tell a real
`server.boot()` crash apart from the self-hosted CI runner (4 cores, shared
with other jobs) just being momentarily oversubscribed. A private-repo PR
fixed the symptom in test_http_concurrency.py alone (30s budget + propagate
the real startup exception instead of a generic timeout message) and the
exact same failure immediately reappeared in a different file, because it
was never one fix -- it was 13 copies of the same bug. This module is the
ONE place that mechanic lives now; every affected fixture delegates to it.

Deliberately NOT extracted here: per-file env setup, tokens, `_start`
signatures, and provisioning order. Those genuinely differ across files --
e.g. some fixtures set `ERGO_SUPERADMIN_TOKEN` and others
deliberately do not, several tests override env AFTER the fixture runs and
rely on `server.boot()` reading env lazily when `_start()` is actually
called, and the bound HTTP server class itself varies (`HTTPServer` vs
`BoundedThreadingHTTPServer`) -- so those all stay put in each file's own
fixture, which now delegates only the thread-startup+readiness part below.
"""
from __future__ import annotations

import threading
from typing import Callable, TypeVar

_T = TypeVar("_T")

# "Did the server thread come up at all" budget -- NOT a performance
# assertion. Widened from the original 5s to 30s (private-repo PR #154) so it never fires
# on a loaded/oversubscribed self-hosted CI runner; a real boot failure is
# surfaced via the re-raised exception below, not this timeout.
READY_TIMEOUT_S = 30.0


def start_server_thread(boot_and_bind: Callable[[], _T]) -> _T:
    """Run `boot_and_bind` on a background thread, wait for it to signal
    readiness, then start serving.

    `boot_and_bind` must perform `server.boot()`, any same-thread setup a
    caller needs to run before the socket starts accepting (e.g. some
    fixtures provision tenants here, deliberately on the same thread as
    `boot()` since store.py's sqlite3 connections are thread-bound), and
    construct + return the bound HTTP server instance -- callers
    deliberately differ on which server class that is, so this helper
    doesn't pick one for them.

    Returns whatever `boot_and_bind` returned (the bound server) once
    startup has succeeded, with `serve_forever(poll_interval=0.01)` already
    kicked off on the background thread. A startup exception raised inside
    `boot_and_bind` is re-raised here, chained via `from`, so a real boot
    error is distinguishable from a startup timeout -- this is the private-repo PR #154
    behavior, migrated here rather than redesigned.
    """
    ready = threading.Event()
    box: dict = {}

    def _serve() -> None:
        try:
            box["result"] = boot_and_bind()
        except BaseException as exc:  # noqa: BLE001 - propagate to the caller below
            box["error"] = exc
        finally:
            ready.set()
        if "result" in box:
            box["result"].serve_forever(poll_interval=0.01)

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    if not ready.wait(timeout=READY_TIMEOUT_S):
        raise AssertionError(
            f"server thread failed to start (timed out after {READY_TIMEOUT_S:.0f}s)"
        )
    if "error" in box:
        raise RuntimeError(
            f"server thread raised during startup: {box['error']!r}"
        ) from box["error"]
    return box["result"]
