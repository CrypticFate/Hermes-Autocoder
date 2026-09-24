import yaml
from typer.testing import CliRunner

from autocoder.cli import app


def test_initialization_is_paused_and_budget_is_explicit(tmp_path, settings):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(settings.model_dump(mode="json")))
    runner = CliRunner()
    def run(*args):
        return runner.invoke(app, ["--config", str(config), *args])
    result = run("init")
    assert result.exit_code == 0, result.output
    result = run("status")
    assert '"paused": true' in result.output
    assert '"daily_usd": 10.0' in result.output
    assert run("budget", "set", "nan", "100").exit_code != 0
    assert run("budget", "set", "10", "100").exit_code == 0
    assert run("init").exit_code == 0
    assert '"daily_usd": 10.0' in run("status").output
