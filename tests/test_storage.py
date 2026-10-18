import json
from datetime import datetime, timezone

import pytest

from portfolio_monitor.db import Repository
from portfolio_monitor.market import CATALOG


def quote(day, value=100):
    return dict(product_id="us:AAPL", metric="price", day=day, source="test",
                value=value, fetched_at="2026-10-18T00:00:00+00:00")


def test_seven_calendar_days_and_cloud_retention(repo):
    repo.save_quotes([quote("2026-10-11"), quote("2026-10-12"), quote("2026-10-18")])
    assert [r["day"] for r in repo.cache.query("SELECT day FROM quotes ORDER BY day")] == ["2026-10-12", "2026-10-18"]
    assert len(repo.cloud.query("SELECT * FROM quotes")) == 3
    assert len(repo.history("us:AAPL", "2026-01-01", "2026-10-18")) == 3
    assert len(repo.cache.query("SELECT * FROM quotes")) == 2


def test_rebuild_after_cache_loss(repo):
    repo.add_product(CATALOG[2], watch=True)
    repo.save_settings({"timezone": "Asia/Tokyo", "time": "10:00", "enabled": True})
    repo.save_quotes([quote("2026-10-10"), quote("2026-10-18")])
    repo.cache.batch([("DELETE FROM quotes", ()), ("DELETE FROM watchlist", ())])
    repo.rebuild()
    assert len(repo.cache.query("SELECT * FROM quotes")) == 1
    assert repo.cache.query("SELECT * FROM watchlist")[0]["product_id"] == "us:AAPL"
    assert json.loads(repo.cache.query("SELECT payload FROM settings")[0]["payload"])["time"] == "10:00"


def test_cloud_failure_does_not_populate_cache(repo, monkeypatch):
    def fail(*args):
        raise RuntimeError("cloud unavailable")
    monkeypatch.setattr(repo.cloud, "batch", fail)
    with pytest.raises(RuntimeError):
        repo.save_quotes([quote("2026-10-18")])
    assert repo.cache.query("SELECT * FROM quotes") == []


def test_corrections_are_idempotent_and_report_is_immutable(repo):
    repo.save_quotes([quote("2026-10-18", 100)])
    repo.save_report("2026-10-18", {"value": 100})
    repo.save_quotes([quote("2026-10-18", 101)])
    repo.save_report("2026-10-18", {"value": 101})
    assert repo.cloud.query("SELECT value FROM quotes") == [{"value": 101.0}]
    assert repo.report("2026-10-18") == {"value": 100}
    assert repo.cache.query("SELECT value FROM quotes") == [{"value": 101.0}]


def test_timezone_boundary(repo):
    repo.now = lambda: datetime(2026, 10, 18, 23, 30, tzinfo=timezone.utc)
    assert repo.today().isoformat() == "2026-10-19"
    repo.save_settings({"timezone": "America/New_York", "time": "09:00", "enabled": False})
    assert repo.today().isoformat() == "2026-10-18"


def test_export_restore_to_empty_database(repo, tmp_path):
    from portfolio_monitor.config import Config
    repo.add_product(CATALOG[2], watch=True)
    repo.save_quotes([quote("2026-10-01"), quote("2026-10-18")])
    data = repo.export()
    other = Repository(Config(demo=True, cache_path=str(tmp_path / "new-cache.db"),
                              demo_history_path=str(tmp_path / "new-history.db")), now=repo.now)
    other.restore(data)
    assert len(other.cloud.query("SELECT * FROM quotes")) == 2
    assert len(other.cache.query("SELECT * FROM quotes")) == 1
    with pytest.raises(ValueError, match="空数据库"):
        other.restore(data)


def test_lock_blocks_other_owner_and_checks_release(repo):
    assert repo.acquire("report", "first")
    assert not repo.acquire("report", "second")
    repo.release("report", "second")
    assert not repo.acquire("report", "second")
    repo.release("report", "first")
    assert repo.acquire("report", "second")


def test_local_cache_error_preserves_cloud_success(repo, monkeypatch):
    monkeypatch.setattr(repo.cache, "batch", lambda _: (_ for _ in ()).throw(OSError("disk full")))
    repo.save_quotes([quote("2026-10-18")])
    assert repo.cache_warning
    assert repo.cloud.query("SELECT value FROM quotes") == [{"value": 100.0}]


def test_libsql_driver_schema_and_transactions(tmp_path):
    import libsql
    conn = libsql.connect(str(tmp_path / "driver.db"))
    conn.execute("CREATE TABLE checks (id TEXT PRIMARY KEY, value REAL)")
    conn.commit()
    conn.execute("INSERT INTO checks VALUES (?,?)", ("a", 1))
    conn.rollback()
    assert conn.execute("SELECT * FROM checks").fetchall() == []
    conn.execute("INSERT INTO checks VALUES (?,?)", ("a", 2))
    conn.commit()
    cursor = conn.execute("SELECT * FROM checks")
    assert cursor.description[0][0] == "id"
    assert cursor.fetchall() == [("a", 2.0)]
    conn.close()


def test_expired_reports_and_runs_are_only_removed_locally(repo):
    old_day = "2026-10-11"
    repo.cloud.batch([("INSERT INTO reports VALUES (?,?)", (old_day, "{}")),
                      ("INSERT INTO runs VALUES (?,?,?)", ("old", old_day, "{}"))])
    repo.cache.batch([("INSERT INTO reports VALUES (?,?)", (old_day, "{}")),
                      ("INSERT INTO runs VALUES (?,?,?)", ("old", old_day, "{}"))])
    repo.prune()
    for table in ("reports", "runs"):
        assert repo.cache.query(f"SELECT * FROM {table}") == []
        assert len(repo.cloud.query(f"SELECT * FROM {table}")) == 1


def test_rebuild_failure_leaves_existing_cache(repo, monkeypatch):
    repo.save_quotes([quote("2026-10-18")])
    monkeypatch.setattr(repo.cloud, "query", lambda *a: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(OSError):
        repo.rebuild()
    assert len(repo.cache.query("SELECT * FROM quotes")) == 1


def test_migration_version_persists_and_recent_report_uses_cache(repo, monkeypatch):
    repo.cloud.migrate()
    assert repo.cloud.query("SELECT version FROM schema_versions") == [{"version": 1}]
    repo.save_report("2026-10-18", {"day": "2026-10-18"})
    original = repo.cloud.query
    def query(sql, args=()):
        if "FROM reports" in sql:
            raise AssertionError("recent report should use cache")
        return original(sql, args)
    monkeypatch.setattr(repo.cloud, "query", query)
    assert repo.report("2026-10-18")["day"] == "2026-10-18"


def test_lease_can_renew_only_for_current_owner(repo):
    assert repo.acquire("report", "first")
    assert repo.renew("report", "first")
    assert not repo.renew("report", "second")
    repo.cloud.batch([("UPDATE locks SET expires_at=0 WHERE id='report'", ())])
    assert not repo.renew("report", "first")
