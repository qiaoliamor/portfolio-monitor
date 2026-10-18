import logging
from datetime import timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from .market import CATALOG, ProviderError

log = logging.getLogger(__name__)


def seed(repo):
    for p in CATALOG:
        if p["overview"] and p["id"] not in {x["id"] for x in repo.products()}:
            repo.add_product(p)
    if repo.config.demo and not repo.watchlist():
        for p in CATALOG:
            if p["id"] in ("us:AAPL", "cn:510300.SH", "fund:000001.OF"):
                repo.add_product(p, watch=True)


def collect(repo, market, scheduled=False):
    settings = repo.settings()
    now = repo.now().astimezone(ZoneInfo(settings["timezone"]))
    day = now.date().isoformat()
    if scheduled and (not settings["enabled"] or now.strftime("%H:%M") < settings["time"]):
        return {"status": "not_due"}
    owner = str(uuid4())
    key = "collection"
    if not repo.acquire(key, owner):
        return {"status": "busy"}
    try:
        if scheduled and repo.report(day):
            return {"status": "already_exists", "day": day}
        run = {"id": now.isoformat() + ":" + owner, "day": day,
               "status": "running", "errors": [], "ok": 0, "started_at": now.isoformat()}
        repo.save_run(run)
        watch = repo.watchlist()
        products = [p for p in repo.products() if p["overview"] or p["id"] in watch]
        cards = []
        for p in products:
            if not repo.renew(key, owner):
                raise RuntimeError("Task lease lost")
            error = None
            try:
                quotes = market.fetch(p, now.date())
                repo.save_quotes(quotes)
                run["ok"] += 1
            except Exception as exc:
                # Provider exceptions deliberately exclude credential-bearing URLs.
                error = str(exc) if isinstance(exc, ProviderError) else "本次更新失败；请检查数据源配置、权限与额度"
                log.warning("Collection failed for product %s", p["id"])
                run["errors"].append({"product": p["name"], "reason": error})
            rows = repo.history(p["id"], (now.date() - timedelta(days=366)).isoformat(), day)
            last = rows[-1] if rows else None
            previous = rows[-2] if len(rows) > 1 else None
            delta = None
            if last and previous:
                delta = ((last["value"] - previous["value"]) * 100 if p["metric"] == "yield"
                         else (last["value"] / previous["value"] - 1) * 100 if previous["value"] else None)
            cards.append({"product": p, "latest": last, "change": delta,
                          "change_unit": "bp" if p["metric"] == "yield" else "%",
                          "watch": p["id"] in watch, "error": error,
                          "stale": bool(last and last["day"] < day)})
        run["status"] = "partial" if run["errors"] else "success"
        if run["ok"] == 0:
            run["status"] = "failed"
        run["finished_at"] = repo.now().isoformat()
        repo.save_run(run)
        if run["ok"]:
            report = {"day": day, "generated_at": now.isoformat(), "timezone": settings["timezone"],
                      "demo": repo.config.demo, "status": run["status"], "cards": cards,
                      "errors": run["errors"]}
            repo.save_report(day, report)
        return run
    except Exception:
        log.error("Cloud collection or persistence failed; task not confirmed successful")
        if "run" in locals():
            run["status"] = "failed"
            run["errors"].append({"product": "任务", "reason": "云端保存或任务租约失败，未确认成功"})
            try:
                repo.save_run(run)
            except Exception:
                log.error("Cloud unavailable; failure record could not be persisted")
        raise
    finally:
        try:
            repo.prune()
        except Exception:
            log.warning("Cache cleanup could not finish")
        try:
            repo.release(key, owner)
        except Exception:
            log.warning("Lease release failed; lease will expire automatically")
