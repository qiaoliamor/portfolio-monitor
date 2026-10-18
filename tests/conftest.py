from datetime import datetime, timezone

import pytest

from portfolio_monitor.config import Config
from portfolio_monitor.db import Repository


@pytest.fixture
def repo(tmp_path):
    config = Config(cache_path=str(tmp_path / "cache.db"), demo=True,
                    demo_history_path=str(tmp_path / "history.db"), secret="test-secret")
    return Repository(config, now=lambda: datetime(2026, 10, 18, 1, tzinfo=timezone.utc))
