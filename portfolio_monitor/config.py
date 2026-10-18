import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    cache_path: str = "data/cache.db"
    remote_url: str = ""
    remote_token: str = ""
    demo: bool = False
    demo_history_path: str = "data/demo-history.db"
    secret: str = ""
    password_hash: str = ""
    production: bool = False

    @classmethod
    def load(cls):
        load_dotenv()
        return cls(
            cache_path=os.getenv("CACHE_PATH", "data/cache.db"),
            remote_url=os.getenv("TURSO_DATABASE_URL", ""),
            remote_token=os.getenv("TURSO_AUTH_TOKEN", ""),
            demo=os.getenv("DEMO_MODE", "false").lower() == "true",
            demo_history_path=os.getenv("DEMO_HISTORY_PATH", "data/demo-history.db"),
            secret=os.getenv("SESSION_SECRET", ""),
            password_hash=os.getenv("ADMIN_PASSWORD_HASH", ""),
            production=os.getenv("APP_ENV") == "production",
        )

    def validate(self):
        if self.production and (len(self.secret) < 32 or not self.password_hash):
            raise ValueError("生产环境必须配置 SESSION_SECRET（至少32字符）与 ADMIN_PASSWORD_HASH")
        if self.production and self.demo:
            raise ValueError("生产环境不能启用演示数据")
        if self.demo and Path(self.cache_path).resolve() == Path(self.demo_history_path).resolve():
            raise ValueError("演示历史库与缓存必须使用不同文件")
