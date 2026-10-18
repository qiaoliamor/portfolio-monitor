"""Independent SQLite cache and authoritative libSQL store; no replication."""
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config

log = logging.getLogger(__name__)
DEFAULT_SETTINGS = {"timezone": "Asia/Tokyo", "time": "09:00", "enabled": False}
TABLES = ("products", "watchlist", "settings", "quotes", "reports", "runs")
COLUMNS = {
    "products": ("id", "payload"), "watchlist": ("product_id",),
    "settings": ("id", "payload"),
    "quotes": ("product_id", "metric", "day", "source", "value", "fetched_at"),
    "reports": ("day", "payload"), "runs": ("id", "day", "payload"),
}


class CloudUnavailable(RuntimeError):
    pass


class Store:
    def __init__(self, path=None, url=None, token=None):
        self.path, self.url, self.token = path, url, token
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self):
        if self.path:
            conn = sqlite3.connect(self.path, timeout=15)
        else:
            import libsql
            conn = libsql.connect(database=self.url, auth_token=self.token)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _retry(self, action):
        for attempt in range(3):
            try:
                return action()
            except Exception:
                if self.path or attempt == 2:
                    raise
                time.sleep(0.5 * (2**attempt))

    def query(self, sql, args=()):
        def execute():
            with self.connection() as c:
                cursor = c.execute(sql, args)
                if cursor.description is None:
                    return []
                names = [x[0] for x in cursor.description]
                return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
        return self._retry(execute)

    def batch(self, statements):
        statements = list(statements)
        def execute():
            with self.connection() as c:
                for sql, args in statements:
                    c.execute(sql, args)
        return self._retry(execute)

    def migrate(self):
        # Each numbered file runs transactionally, identically on both engines.
        self.batch([("CREATE TABLE IF NOT EXISTS schema_versions (version INTEGER PRIMARY KEY)", ())])
        versions = {row["version"] for row in self.query("SELECT version FROM schema_versions")}
        for file in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
            if int(file.stem) in versions:
                continue
            self.batch([(s.strip(), ()) for s in file.read_text().split(";") if s.strip()])


class Repository:
    def __init__(self, config: Config, now=None, cloud=None):
        self.config = config
        self.now = now or (lambda: datetime.now().astimezone())
        self.cache = Store(path=config.cache_path)
        self.cache.migrate()
        if cloud:
            self.cloud = cloud
        elif config.demo:
            self.cloud = Store(path=config.demo_history_path)
        elif config.remote_url and config.remote_token:
            self.cloud = Store(url=config.remote_url, token=config.remote_token)
        else:
            raise CloudUnavailable("尚未配置 Turso。请设置云端凭证，或显式启用 DEMO_MODE=true。")
        self.cloud.migrate()
        self.cache_warning = False
        self.rebuild()

    def settings(self):
        rows = self.cloud.query("SELECT payload FROM settings WHERE id='app'")
        return json.loads(rows[0]["payload"]) if rows else dict(DEFAULT_SETTINGS)

    def today(self):
        return self.now().astimezone(ZoneInfo(self.settings()["timezone"])).date()

    def cutoff(self):
        return (self.today() - timedelta(days=6)).isoformat()

    def _cache(self, statements):
        try:
            self.cache.batch(statements)
            return True
        except Exception:
            self.cache_warning = True
            log.warning("Local cache write failed; cloud remains authoritative")
            return False

    def save_settings(self, settings):
        ZoneInfo(settings["timezone"])
        settings = {**settings, "time": datetime.strptime(settings["time"], "%H:%M").strftime("%H:%M")}
        statement = ("INSERT OR REPLACE INTO settings VALUES ('app', ?)",
                     (json.dumps(settings),))
        self.cloud.batch([statement])
        self._cache([statement])
        self.prune()

    def products(self):
        return [json.loads(x["payload"]) for x in self.cloud.query(
            "SELECT payload FROM products ORDER BY id")]

    def watchlist(self):
        return {x["product_id"] for x in self.cloud.query("SELECT product_id FROM watchlist")}

    def add_product(self, product, watch=False):
        statements = [("INSERT OR REPLACE INTO products VALUES (?,?)",
                       (product["id"], json.dumps(product, ensure_ascii=False)))]
        if watch:
            statements.append(("INSERT OR IGNORE INTO watchlist VALUES (?)", (product["id"],)))
        self.cloud.batch(statements)
        self._cache(statements)

    def unwatch(self, product_id):
        statements = [("DELETE FROM watchlist WHERE product_id=?", (product_id,))]
        self.cloud.batch(statements)
        self._cache(statements)

    def save_quotes(self, quotes):
        cutoff = self.cutoff()
        sql = "INSERT OR REPLACE INTO quotes VALUES (?,?,?,?,?,?)"
        statements = [(sql, tuple(q[c] for c in COLUMNS["quotes"])) for q in quotes]
        self.cloud.batch(statements)
        self._cache([s for s, q in zip(statements, quotes, strict=True)
                     if q["day"] >= cutoff])
        self.prune()

    def history(self, product_id, start, end):
        cutoff = self.cutoff()
        # Query authority to detect holes/corrections; cache only the recent subset.
        rows = self.cloud.query(
            "SELECT * FROM quotes WHERE product_id=? AND day>=? AND day<=? ORDER BY day",
            (product_id, start, end))
        self._cache([("INSERT OR REPLACE INTO quotes VALUES (?,?,?,?,?,?)",
                      tuple(q[c] for c in COLUMNS["quotes"]))
                     for q in rows if q["day"] >= cutoff])
        return rows

    def _cached_query(self, sql, args=()):
        if self.cache_warning:
            return []
        try:
            return self.cache.query(sql, args)
        except (sqlite3.Error, OSError):
            self.cache_warning = True
            log.warning("Local cache read failed; falling back to cloud")
            return []

    def recent(self, product_id):
        cutoff = self.cutoff()
        rows = self._cached_query(
            "SELECT * FROM quotes WHERE product_id=? AND day>=? ORDER BY day",
            (product_id, cutoff))
        if rows:
            return rows
        return self.history(product_id, cutoff, self.today().isoformat())

    def save_report(self, day, payload):
        # Reports are immutable snapshots, even on repeated manual collection.
        statement = ("INSERT OR IGNORE INTO reports VALUES (?,?)",
                     (day, json.dumps(payload, ensure_ascii=False)))
        self.cloud.batch([statement])
        if day >= self.cutoff():
            actual = json.loads(self.cloud.query(
                "SELECT payload FROM reports WHERE day=?", (day,))[0]["payload"])
            self._cache([("INSERT OR REPLACE INTO reports VALUES (?,?)",
                          (day, json.dumps(actual, ensure_ascii=False)))])

    def report(self, day):
        if day >= self.cutoff() and not self.cache_warning:
            cached = self._cached_query("SELECT payload FROM reports WHERE day=?", (day,))
            if cached:
                return json.loads(cached[0]["payload"])
        rows = self.cloud.query("SELECT payload FROM reports WHERE day=?", (day,))
        return json.loads(rows[0]["payload"]) if rows else None

    def reports(self):
        return [json.loads(x["payload"]) for x in self.cloud.query(
            "SELECT payload FROM reports ORDER BY day DESC LIMIT 366")]

    def save_run(self, run):
        statement = ("INSERT OR REPLACE INTO runs VALUES (?,?,?)",
                     (run["id"], run["day"], json.dumps(run, ensure_ascii=False)))
        self.cloud.batch([statement])
        if run["day"] >= self.cutoff():
            self._cache([statement])

    def runs(self):
        return [json.loads(x["payload"]) for x in self.cloud.query(
            "SELECT payload FROM runs ORDER BY day DESC, id DESC LIMIT 20")]

    def acquire(self, key, owner, seconds=1200):
        now = time.time()
        self.cloud.batch([
            ("DELETE FROM locks WHERE id=? AND expires_at<?", (key, now)),
            ("INSERT OR IGNORE INTO locks VALUES (?,?,?)", (key, owner, now + seconds))])
        rows = self.cloud.query("SELECT owner FROM locks WHERE id=?", (key,))
        return bool(rows and rows[0]["owner"] == owner)

    def renew(self, key, owner, seconds=1200):
        now = time.time()
        self.cloud.batch([("UPDATE locks SET expires_at=? WHERE id=? AND owner=? AND expires_at>?",
                           (now + seconds, key, owner, now))])
        rows = self.cloud.query("SELECT owner, expires_at FROM locks WHERE id=?", (key,))
        return bool(rows and rows[0]["owner"] == owner and rows[0]["expires_at"] > now)

    def release(self, key, owner):
        self.cloud.batch([("DELETE FROM locks WHERE id=? AND owner=?", (key, owner))])

    def prune(self):
        cutoff = self.cutoff()
        self._cache([(f"DELETE FROM {table} WHERE day<?", (cutoff,))
                     for table in ("quotes", "reports", "runs")])

    def rebuild(self):
        # Load everything needed first: failed fetches never clear usable local data.
        cutoff = self.cutoff()
        statements = []
        for table in TABLES:
            sql = f"SELECT * FROM {table}"
            args = ()
            if table in ("quotes", "reports", "runs"):
                sql += " WHERE day>=?"
                args = (cutoff,)
            rows = self.cloud.query(sql, args)
            statements.append((f"DELETE FROM {table}", ()))
            cols = COLUMNS[table]
            statements.extend((f"INSERT INTO {table} VALUES ({','.join('?' for _ in cols)})",
                               tuple(row[c] for c in cols)) for row in rows)
        if self._cache(statements):
            self.cache_warning = False

    def export(self):
        with self.cloud.connection() as c:
            # Consistent snapshot across all tables.
            c.execute("BEGIN")
            tables = {}
            for table in TABLES:
                cur = c.execute(f"SELECT * FROM {table}")
                cols = [x[0] for x in cur.description]
                tables[table] = [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
        return {"format": 1, "exported_at": self.now().isoformat(), "tables": tables}

    def restore(self, backup):
        if backup.get("format") != 1 or set(backup.get("tables", {})) != set(TABLES):
            raise ValueError("备份格式不正确")
        if any(self.cloud.query(f"SELECT 1 FROM {t} LIMIT 1") for t in TABLES):
            raise ValueError("恢复只允许写入空数据库；请先创建新的云端库")
        statements = []
        for table in TABLES:
            cols = COLUMNS[table]
            for row in backup["tables"][table]:
                statements.append((
                    f"INSERT INTO {table} VALUES ({','.join('?' for _ in cols)})",
                    tuple(row[col] for col in cols)))
        self.cloud.batch(statements)
        self.rebuild()
