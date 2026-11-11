import asyncio

import pytest
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core.config import settings
from app.core.upload_guard import _client_fingerprint
from app.main import _client_address


def scope_for(peer, header):
    return {"type": "http", "method": "POST", "path": "/api/v1/auth/login",
            "scheme": "http", "client": (peer, 1234), "headers": [(b"x-forwarded-for", header)]}


@pytest.mark.parametrize("legacy_setting", [False, True])
def test_raw_forwarded_header_cannot_change_rate_limit_identity(monkeypatch, legacy_setting):
    monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", legacy_setting)
    first = scope_for("198.51.100.10", b"192.0.2.1")
    forged = scope_for("198.51.100.10", b"192.0.2.99, 192.0.2.100")
    assert _client_address(Request(first)) == _client_address(Request(forged)) == "198.51.100.10"
    assert _client_fingerprint(first) == _client_fingerprint(forged)
    assert _client_fingerprint(first) != _client_fingerprint(scope_for("198.51.100.11", b"192.0.2.1"))


@pytest.mark.parametrize("peer,expected", [("10.0.0.2", "198.51.100.10"),
                                          ("203.0.113.10", "203.0.113.10")])
def test_asgi_server_resolves_only_configured_proxy_peers(peer, expected):
    observed = []

    async def downstream(scope, receive, send):
        observed.append(_client_address(Request(scope)))

    middleware = ProxyHeadersMiddleware(downstream, trusted_hosts=["10.0.0.2"])
    scope = scope_for(peer, b"192.0.2.99, 198.51.100.10")
    asyncio.run(middleware(scope, None, None))
    assert observed == [expected]
