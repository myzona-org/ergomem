"""Thin HTTP client for the Ergo API, duplicated from `ergo/mcp/server.py`.

DELIBERATELY not imported from `ergo.mcp.server`: this plugin ships
standalone into a user's Hermes installation
(`~/.hermes/plugins/memory/ergo/`, or a pip package) -- a different Python
environment than this repo's own venv, with no `ergo-memory` package on its
path. Depending on it would drag in the `engine` extra (fastembed,
onnxruntime, ...) just to reuse an ~80-line httpx wrapper around 9 HTTP
calls, and would require this plugin's runtime to install this whole repo as
a dependency. Duplicating a thin client is cheaper and safer than coupling a
Hermes plugin's dependency footprint to the engine's.

Mirrors the shape and 409-handling of `ergo.mcp.server.ErgoClient` (same
`_GUARDED_OK` treatment) so the two stay easy to eyeball-diff if the HTTP API
changes.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Union

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8788"
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
# prefetch()/recall runs synchronously before every turn -- fail fast rather
# than stalling the user's chat turn on an unreachable or slow engine.
PREFETCH_TIMEOUT = httpx.Timeout(4.0, connect=2.0)


class ErgoError(RuntimeError):
    """A non-success, non-conflict response from the engine."""

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"ergo HTTP {status_code}: {body}")


class ErgoClient:
    """Thin httpx wrapper. One method per endpoint the plugin uses.

    409 (conflict) is NOT an error here -- it is a documented outcome of the
    guarded write paths (`/remember`, `/learn`, `/supersede`), returned as a
    normal body so the caller can surface the conflict. Every other non-2xx
    raises `ErgoError`.
    """

    _GUARDED_OK = (200, 409)
    _OK = (200,)

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        timeout: Union[httpx.Timeout, float] = DEFAULT_TIMEOUT,
    ):
        self._base_url = (base_url or DEFAULT_API_URL).rstrip("/")
        self._token = token
        self._client = httpx.Client(base_url=self._base_url, transport=transport, timeout=timeout)

    @property
    def base_url(self) -> str:
        return self._base_url

    def close(self) -> None:
        self._client.close()

    def _headers(self, *, auth: bool = True) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _parse(self, resp: httpx.Response, ok: tuple) -> Dict[str, Any]:
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        if resp.status_code in ok:
            return body if isinstance(body, dict) else {"data": body}
        raise ErgoError(resp.status_code, body)

    def health(self) -> Dict[str, Any]:
        resp = self._client.get("/health", headers=self._headers(auth=False))
        return self._parse(resp, self._OK)

    def remember(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._client.post("/remember", headers=self._headers(), json=payload)
        return self._parse(resp, self._GUARDED_OK)

    def learn(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._client.post("/learn", headers=self._headers(), json=payload)
        return self._parse(resp, self._GUARDED_OK)

    def ingest(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._client.post("/ingest", headers=self._headers(), json=payload)
        return self._parse(resp, self._OK)

    def supersede(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._client.post("/supersede", headers=self._headers(), json=payload)
        return self._parse(resp, self._GUARDED_OK)

    def retract(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._client.post("/retract", headers=self._headers(), json=payload)
        return self._parse(resp, self._OK)

    def recall(
        self,
        params: Dict[str, Any],
        *,
        timeout: Optional[Union[httpx.Timeout, float]] = None,
    ) -> Dict[str, Any]:
        """`timeout` defaults to the client's normal timeout (used by the
        deliberate `ergo_recall` tool call). `prefetch()` passes
        `PREFETCH_TIMEOUT` explicitly -- that short deadline belongs to the
        automatic per-turn path, not every caller of this method."""
        kwargs: Dict[str, Any] = {"headers": self._headers(), "params": params}
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = self._client.get("/recall", **kwargs)
        return self._parse(resp, self._OK)

    def why(self, params: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._client.get("/why", headers=self._headers(), params=params)
        return self._parse(resp, self._OK)

    def active(self, params: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._client.get("/active", headers=self._headers(), params=params)
        return self._parse(resp, self._OK)

    def history(self, params: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._client.get("/history", headers=self._headers(), params=params)
        return self._parse(resp, self._OK)
