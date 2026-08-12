"""Shared test-only defaults for HTTP clients used by integration tests."""

from __future__ import annotations

import functools
import os

import httpx
import pytest


_DEFAULT_HTTP_TIMEOUT = 30.0
_HTTP_HELPERS = ("get", "post", "put", "delete", "request", "stream")


def _test_http_timeout() -> float:
    value = os.environ.get("ERGO_TEST_HTTP_TIMEOUT")
    return _DEFAULT_HTTP_TIMEOUT if value is None else float(value)


@pytest.fixture(autouse=True)
def _generous_httpx_timeout(monkeypatch):
    """Raise httpx's test default without clobbering caller-supplied values.

    httpx's helper defaults are bound when the module is imported, so changing
    DEFAULT_TIMEOUT_CONFIG cannot affect them. Wrapping the helpers instead
    makes the default explicit at each call while retaining timeout= callers.
    The Client constructor is wrapped for tests that instantiate clients
    directly; the environment is read at call time for per-test overrides.
    """

    for name in _HTTP_HELPERS:
        original = getattr(httpx, name)

        @functools.wraps(original)
        def helper(*args, _original=original, **kwargs):
            kwargs.setdefault("timeout", _test_http_timeout())
            return _original(*args, **kwargs)

        monkeypatch.setattr(httpx, name, helper)

    for client_cls in (httpx.Client, httpx.AsyncClient):
        original_init = client_cls.__init__

        @functools.wraps(original_init)
        def client_init(self, *args, _original_init=original_init, **kwargs):
            kwargs.setdefault("timeout", _test_http_timeout())
            return _original_init(self, *args, **kwargs)

        monkeypatch.setattr(client_cls, "__init__", client_init)
