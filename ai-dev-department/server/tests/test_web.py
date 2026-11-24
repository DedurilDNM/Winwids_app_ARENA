"""Web client is served by the API at /app/ with strict security headers."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_web_app_served_with_security_headers() -> None:
    with TestClient(app) as client:
        res = client.get("/app/")
        assert res.status_code == 200
        assert "<title>AIDD</title>" in res.text
        csp = res.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "unsafe-inline" not in csp
        assert "font-src 'self' https://fonts.gstatic.com" in csp  # the app's fonts, nothing else
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


def test_code_addresses_carry_the_build_version():
    """A new build has new addresses for its code, so a cache on the way (browser, CDN)
    cannot serve old code after an update; every module imports the same versioned URLs."""
    import re

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        page = client.get("/app/").text
        v = re.search(r'src="js/app\.js\?v=([0-9a-f]{10})"', page).group(1)
        assert f'href="css/app.css?v={v}"' in page
        chat = client.get(f"/app/js/views/chat.js?v={v}")
        assert chat.status_code == 200 and chat.headers["cache-control"] == "no-cache"
        assert f'from "../api.js?v={v}"' in chat.text and f'from "../models.js?v={v}"' in chat.text
        assert not re.search(r'from "\.{1,2}/[^"?]+\.js"', chat.text)
        assert "?v=" not in client.get("/app/sw.js").text
