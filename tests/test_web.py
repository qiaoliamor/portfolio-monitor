import re

from fastapi.testclient import TestClient

from portfolio_monitor.auth import hash_password, verify_password
from portfolio_monitor.market import Market
from portfolio_monitor.web import create_app


def token(html):
    return re.search(r'name="token" value="([^"]+)"', html).group(1)


def test_login_csrf_navigation_and_watchlist(repo):
    app = create_app(repo.config, repo=repo, market=Market(demo=True))
    with TestClient(app) as client:
        assert client.get("/", follow_redirects=False).status_code == 303
        page = client.get("/login")
        client.post("/login", data={"token": token(page.text)})
        for path in ("/", "/watchlist", "/reports", "/settings", "/product?id=us:AAPL", "/report/2026-10-18"):
            response = client.get(path)
            assert response.status_code == 200, response.text
            assert "演示" in response.text
        response = client.get("/watchlist?q=AAPL")
        client.post("/watchlist/add", data={"token": token(response.text), "product_id": "us:AAPL"})
        assert "us:AAPL" in repo.watchlist()
        client.post("/watchlist/remove", data={"token": token(response.text), "product_id": "us:AAPL"})
        assert "us:AAPL" not in repo.watchlist()
        assert client.get("/api/chart?id=us:AAPL").json()
        blocked = client.post("/settings", data={"timezone": "Asia/Tokyo", "report_time": "09:00", "token": "bad"})
        assert "表单已过期" in blocked.text


def test_password_protection_and_invalid_timezone(repo):
    repo.config.demo = False
    repo.config.password_hash = hash_password("correct-password-123")
    with TestClient(create_app(repo.config, repo=repo, market=Market(demo=True))) as client:
        login = client.get("/login")
        bad = client.post("/login", data={"token": token(login.text), "password": "wrong"})
        assert "密码不正确" in bad.text
        assert client.get("/api/chart?id=us:AAPL").status_code == 401
        client.post("/login", data={"token": token(login.text), "password": "correct-password-123"})
        page = client.get("/settings")
        result = client.post("/settings", data={"token": token(page.text), "timezone": "bad", "report_time": "09:00"})
        assert "时区或时间无效" in result.text


def test_password_hash():
    hashed = hash_password("a-strong-password")
    assert verify_password("a-strong-password", hashed)
    assert not verify_password("wrong-password", hashed)


def test_production_requires_password_and_stable_secret(repo):
    import pytest
    repo.config.production = True
    repo.config.demo = False
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        create_app(repo.config, repo=repo)
