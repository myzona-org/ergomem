"""Regression tests for the suite-wide httpx timeout default."""

import asyncio

import httpx
import httpx._api


def test_bare_httpx_post_uses_suite_timeout(monkeypatch):
    seen = {}

    class SpyClient:
        def __init__(self, **kwargs):
            seen["timeout"] = kwargs["timeout"]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, *args, **kwargs):
            return httpx.Response(200)

    monkeypatch.setattr(httpx._api, "Client", SpyClient)
    httpx.post("http://test.invalid")

    assert seen["timeout"] == 30.0


def test_explicit_httpx_timeout_is_preserved(monkeypatch):
    seen = {}

    class SpyClient:
        def __init__(self, **kwargs):
            seen["timeout"] = kwargs["timeout"]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, *args, **kwargs):
            return httpx.Response(200)

    monkeypatch.setattr(httpx._api, "Client", SpyClient)
    httpx.post("http://test.invalid", timeout=0.25)

    assert seen["timeout"] == 0.25


def test_httpx_timeout_can_be_overridden_by_environment(monkeypatch):
    seen = {}

    class SpyClient:
        def __init__(self, **kwargs):
            seen["timeout"] = kwargs["timeout"]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request(self, *args, **kwargs):
            return httpx.Response(200)

    monkeypatch.setattr(httpx._api, "Client", SpyClient)
    monkeypatch.setenv("ERGO_TEST_HTTP_TIMEOUT", "47.5")
    httpx.post("http://test.invalid")

    assert seen["timeout"] == 47.5


def test_bare_async_client_uses_suite_timeout():
    client = httpx.AsyncClient()
    try:
        assert client.timeout.read == 30.0
    finally:
        asyncio.run(client.aclose())
