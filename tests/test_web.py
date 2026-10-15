import re
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest
from werkzeug.security import generate_password_hash

from emlak_bot.config import Config
from emlak_bot.db import Database
from emlak_bot.web import create_app


@pytest.fixture
def env(tmp_path):
    db = Database(str(tmp_path / "w.db"))
    owner = db.add_user(1, "Patron", "owner")
    a1 = db.add_user(2, "Mehmet", "agent")
    a2 = db.add_user(3, "Ayşe", "agent")
    db.set_credentials(owner, "patron", generate_password_hash("patron-pass-1"))
    db.set_credentials(a1, "mehmet", generate_password_hash("mehmet-pass-1"))
    db.set_credentials(a2, "ayse", generate_password_hash("ayse-pass-123"))
    due = (date.today() + timedelta(days=10)).isoformat()
    p1 = db.add_property(agent_id=a1, title="Mehmet'in evi", rent=30000, renewal_date=due)
    p2 = db.add_property(agent_id=a2, title="Ayşe'nin evi", rent=20000, renewal_date=due)
    cfg = Config("", frozenset({1}), "", ZoneInfo("Europe/Istanbul"), 9, 7, "test-secret", 8000, True, False, "")
    app = create_app(db, cfg)
    app.config["TESTING"] = True
    return app, db, p1, p2


def csrf(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(client, user, pw):
    return client.post("/login", data={"username": user, "password": pw, "csrf": csrf(client)})


def test_requires_login(env):
    app, *_ = env
    c = app.test_client()
    assert c.get("/").status_code == 302
    assert c.post("/login", data={"username": "x", "password": "y"}).status_code == 400  # CSRF yok


def test_wrong_password_and_lockout(env):
    app, *_ = env
    c = app.test_client()
    for _ in range(5):
        assert login(c, "mehmet", "yanlis").status_code == 200
    assert login(c, "mehmet", "mehmet-pass-1").status_code == 429  # kilitli


def test_agent_sees_only_own(env):
    app, db, p1, p2 = env
    c = app.test_client()
    assert login(c, "mehmet", "mehmet-pass-1").status_code == 302
    page = c.get("/").get_data(as_text=True)
    assert "Mehmet'in evi" in page.replace("&#39;", "'") and "Ayşe" not in page
    assert c.get(f"/mulk/{p1}").status_code == 200
    assert c.get(f"/mulk/{p2}").status_code == 403
    assert c.get("/aktivite").status_code == 403


def test_owner_sees_all_and_zam_works(env):
    app, db, p1, p2 = env
    c = app.test_client()
    login(c, "patron", "patron-pass-1")
    page = c.get("/").get_data(as_text=True)
    assert "Mehmet" in page and "Ayşe" in page
    r = c.post(f"/mulk/{p1}/zam", data={"value": "%30", "csrf": csrf(c, f"/mulk/{p1}")})
    assert r.status_code == 302
    assert db.get_property(p1)["planned_rent"] == 39000
    assert c.get("/aktivite").status_code == 200


def test_departed_agent_loses_access(env):
    app, db, p1, _ = env
    c = app.test_client()
    login(c, "mehmet", "mehmet-pass-1")
    assert c.get("/").status_code == 200
    db.deactivate_user(db.get_user_by_username("mehmet")["id"])
    assert c.get("/").status_code == 302
    assert login(app.test_client(), "mehmet", "mehmet-pass-1").status_code == 200  # girilemez


def test_add_property_via_web(env):
    app, db, *_ = env
    c = app.test_client()
    login(c, "mehmet", "mehmet-pass-1")
    r = c.post("/ekle", data={"title": "Yeni", "rent": "25.000", "renewal_date": "2027-01-01",
                              "csrf": csrf(c, "/ekle")})
    assert r.status_code == 302
    assert any(p["title"] == "Yeni" and p["rent"] == 25000 for p in db.list_properties())
