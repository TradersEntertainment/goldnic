import asyncio
from datetime import date

import pytest

from emlak_bot import rent as R
from emlak_bot.db import Database
from emlak_bot.messenger import Messenger
from emlak_bot.reminders import build_owner_report, run_reminders


class FakeMessenger(Messenger):
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text))
        return True


def test_parsing():
    assert R.parse_amount("45.000") == 45000
    assert R.parse_amount("45000,50 TL") == 45000.5
    assert R.parse_amount("1.234.567,5") == 1234567.5
    assert R.parse_pct("%30") == 30
    assert R.parse_pct("33,5") == 33.5
    assert R.parse_date("01.11.2026") == date(2026, 11, 1)
    with pytest.raises(ValueError):
        R.parse_amount("abc")
    with pytest.raises(ValueError):
        R.parse_date("32.13.2026")


def test_calc_and_format():
    assert R.apply_pct(30000, 30) == 39000
    assert round(R.pct_change(30000, 39000), 6) == 30
    assert R.money(39000) == "39.000 ₺"
    assert R.pct_str(33.5) == "%33,5" and R.pct_str(25.0) == "%25"
    assert R.add_year(date(2024, 2, 29)) == date(2025, 2, 28)


def test_pick_stage():
    assert R.pick_stage(45) is None
    assert R.pick_stage(30) == 30
    assert R.pick_stage(20) == 23
    assert R.pick_stage(15) == 16
    assert R.pick_stage(10) == 11
    assert R.pick_stage(7) == 8
    assert R.pick_stage(4) == 5
    assert R.pick_stage(1) == 2
    assert R.pick_stage(0) == 0
    assert R.pick_stage(-3) == -1
    assert R.pick_stage(-100) is None


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.add_user(1, "Patron", "owner")
    d.add_user(2, "Mehmet", "agent")
    return d


def _prop(db, days_ahead, today, **kw):
    from datetime import timedelta
    return db.add_property(
        agent_id=2, title="Daire", tenant_name="Ali", rent=30000,
        renewal_date=(today + timedelta(days=days_ahead)).isoformat(),
        last_increase_pct=25, last_increase_amount=6000, **kw)


def test_reminders_sent_once_to_agent_and_owner(db):
    today = date(2026, 10, 15)
    _prop(db, 14, today)
    m = FakeMessenger()
    assert asyncio.run(run_reminders(db, m, today)) == 1
    assert {c for c, _ in m.sent} == {"1", "2"}
    assert "%25" in m.sent[0][1] and "henüz girilmedi" in m.sent[0][1]
    assert asyncio.run(run_reminders(db, m, today)) == 0  # aynı aşama tekrar gitmez


def test_no_reminder_far_ahead(db):
    today = date(2026, 10, 15)
    _prop(db, 90, today)
    assert asyncio.run(run_reminders(db, FakeMessenger(), today)) == 0


def test_departed_agent_portfolio_transfers(db):
    today = date(2026, 10, 15)
    pid = _prop(db, 5, today)
    db.transfer_properties(2, 1)
    db.deactivate_user(2)
    m = FakeMessenger()
    asyncio.run(run_reminders(db, m, today))
    assert {c for c, _ in m.sent} == {"1"}  # sadece sahibe gider
    assert db.get_property(pid)["agent_id"] == 1


def test_owner_report_flags_issues(db):
    today = date(2026, 10, 15)
    _prop(db, -3, today)
    r = build_owner_report(db, today, 7, full=False)
    assert "gecikti" in r and "Mehmet" in r  # gecikmiş + hiç kullanmamış


def test_group_receives_reminder_and_digest(db):
    from emlak_bot.reminders import run_owner_digest
    today = date(2026, 10, 15)
    _prop(db, 3, today)
    m = FakeMessenger()
    assert asyncio.run(run_reminders(db, m, today, group_chat_id="-1001234")) == 1
    assert "-1001234" in {c for c, _ in m.sent}
    m2 = FakeMessenger()
    asyncio.run(run_owner_digest(db, m2, 7, today, group_chat_id="-1001234"))
    assert "-1001234" in {c for c, _ in m2.sent}


def test_setup_code_makes_owner(db):
    from types import SimpleNamespace as NS
    from emlak_bot import handlers

    sent = []

    class Msg:
        async def reply_text(self, text, **kw):
            sent.append(text)

    def make_update(uid):
        return NS(effective_user=NS(id=uid, full_name="Musteri"), effective_chat=NS(type="private"),
                  effective_message=Msg())

    cfg = NS(setup_code="gizli-kod", owner_ids=frozenset(), group_chat_id="")
    ctx = NS(application=NS(bot_data={"db": db, "cfg": cfg}), args=["yanlis"])
    asyncio.run(handlers.cmd_setup(make_update(99), ctx))
    assert db.get_user_by_chat(99) is None  # yanlış kod
    ctx.args = ["gizli-kod"]
    asyncio.run(handlers.cmd_setup(make_update(99), ctx))
    assert db.get_user_by_chat(99)["role"] == "owner"
