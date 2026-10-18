import argparse
import getpass
import json
import logging
import os
from pathlib import Path

from .auth import hash_password
from .config import Config
from .db import Repository
from .market import CATALOG, Market
from .service import collect, seed


def main():
    parser = argparse.ArgumentParser(description="Portfolio Monitor 维护工具")
    parser.add_argument("command", choices=["serve", "collect", "tick", "rebuild", "export", "restore",
                                            "hash-password", "validate-providers"])
    parser.add_argument("--file", help="导出/恢复 JSON 路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    if args.command == "hash-password":
        password = getpass.getpass("新登录密码（至少12位）: ")
        if password != getpass.getpass("再次输入: "):
            parser.error("两次密码不同")
        print(hash_password(password))
        return
    if args.command == "serve":
        import uvicorn
        uvicorn.run("portfolio_monitor.web:create_app", factory=True, host=args.host, port=args.port)
        return
    config = Config.load()
    config.validate()
    if args.command == "validate-providers":
        market = Market(demo=False)
        results = []
        for p in CATALOG:
            try:
                quotes = market.fetch(p)
                results.append({"id": p["id"], "status": "verified", "latest_day": quotes[-1]["day"],
                                "rows": len(quotes)})
            except Exception:
                results.append({"id": p["id"], "status": "unverified",
                                "reason": "需要可用网络、API 密钥及对应产品权限"})
        market.close()
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    repo = Repository(config)
    if args.command in ("collect", "tick"):
        seed(repo)
        market = Market(config.demo)
        try:
            result = collect(repo, market, scheduled=args.command == "tick")
        finally:
            market.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["status"] == "failed":
            raise SystemExit(1)
    elif args.command == "rebuild":
        repo.rebuild()
        if repo.cache_warning:
            parser.exit(1, "本地缓存重建失败；云端历史未受影响，请检查磁盘后重试。\n")
        print("本地七天缓存已重建")
    elif args.command in ("export", "restore"):
        if not args.file:
            parser.error("请提供 --file")
        file = Path(args.file)
        if args.command == "export":
            file.parent.mkdir(parents=True, exist_ok=True)
            with file.open("x", encoding="utf-8") as out:
                os.chmod(file, 0o600)
                json.dump(repo.export(), out, ensure_ascii=False, indent=2)
            print("已导出；请将备份保存在独立安全位置")
        else:
            repo.restore(json.loads(file.read_text()))
            print("已恢复至空历史库并重建缓存")
