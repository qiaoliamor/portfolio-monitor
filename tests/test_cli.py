from types import SimpleNamespace

import pytest

from portfolio_monitor import cli
from portfolio_monitor.config import Config


def test_rebuild_failure_exits_nonzero_without_success_message(monkeypatch, capsys):
    monkeypatch.setattr(cli.Config, "load", lambda: Config())
    monkeypatch.setattr(cli, "Repository", lambda config: SimpleNamespace(
        cache_warning=True, rebuild=lambda: None))
    monkeypatch.setattr("sys.argv", ["portfolio", "rebuild"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 1
    output = capsys.readouterr()
    assert "已重建" not in output.out
    assert "重建失败" in output.err
