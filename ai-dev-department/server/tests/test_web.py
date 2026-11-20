"""Web client is served by the API at /app/ with strict security headers."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_web_app_served_with_security_headers() -> None:
    with TestClient(app) as client:
        res = client.get("/app/")
        assert res.status_code == 200
        assert "AI Dev Department" in res.text
        csp = res.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "unsafe-inline" not in csp
        assert "http://127.0.0.1:6080" in csp  # noVNC iframe origin
        assert res.headers["referrer-policy"] == "no-referrer"
        assert res.headers["cache-control"] == "no-cache"

        for asset in ("js/app.js", "js/api.js", "css/app.css", "manifest.webmanifest", "sw.js", "icon.svg"):
            assert client.get(f"/app/{asset}").status_code == 200, asset


def test_root_redirects_browsers_but_not_api_clients() -> None:
    with TestClient(app) as client:
        res = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
        assert res.status_code == 307 and res.headers["location"] == "/app/"
        assert client.get("/", headers={"accept": "application/json"}).json()["docs"] == "/docs"
        res = client.get("/app", follow_redirects=False)
        assert res.status_code == 307 and res.headers["location"] == "/app/"


def test_api_routes_have_no_web_csp() -> None:
    with TestClient(app) as client:
        assert "content-security-policy" not in client.get("/health").headers
