import httpx

from portfolio_monitor.market import CATALOG, Market
from portfolio_monitor.service import collect, seed


def test_fred_csv_skips_missing_and_uses_actual_dates():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=
        "observation_date,DGS10\n2026-10-14,4.10\n2026-10-15,.\n2026-10-16,4.20\n"))
    market = Market(client=httpx.Client(transport=transport))
    rows = market.fetch(next(p for p in CATALOG if p["symbol"] == "DGS10"))
    assert [x["day"] for x in rows] == ["2026-10-14", "2026-10-16"]
    assert rows[-1]["metric"] == "yield"


def test_partial_failures_and_snapshot(repo):
    seed(repo)
    market = Market(demo=True)
    original = market.fetch
    market.fetch = lambda p, end: original(p, end) if p["source"] == "fred" else (_ for _ in ()).throw(RuntimeError("private url"))
    run = collect(repo, market)
    assert run["status"] == "partial"
    report = repo.report("2026-10-18")
    assert report["errors"] and "private url" not in str(report)
    yields = [c for c in report["cards"] if c["product"]["metric"] == "yield"]
    assert yields[0]["change_unit"] == "bp"
    assert yields[0]["latest"]["day"] == "2026-10-16"
    assert yields[0]["stale"]
    old_snapshot = repo.report("2026-10-18")
    collect(repo, Market(demo=True))
    assert repo.report("2026-10-18") == old_snapshot


def test_disabled_and_duplicate_schedule(repo):
    seed(repo)
    assert collect(repo, Market(demo=True), scheduled=True)["status"] == "not_due"
    repo.save_settings({"timezone": "Asia/Tokyo", "time": "09:00", "enabled": True})
    assert collect(repo, Market(demo=True), scheduled=True)["status"] == "success"
    assert collect(repo, Market(demo=True), scheduled=True)["status"] == "already_exists"


def test_no_valid_data_does_not_create_report(repo):
    seed(repo)
    market = Market(demo=True)
    market.fetch = lambda *a: (_ for _ in ()).throw(RuntimeError("offline"))
    assert collect(repo, market)["status"] == "failed"
    assert repo.report("2026-10-18") is None
    assert repo.runs()[0]["status"] == "failed"
