"""
Tests for user_data/scripts/start_bot.sh.

Runs the entrypoint in a subprocess with controlled environments. Freqtrade
is never actually launched: failure cases exit before the exec line, and the
success case puts a stub `freqtrade` executable on PATH.
"""

import os
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
START_BOT = REPO_ROOT / "user_data" / "scripts" / "start_bot.sh"

REQUIRED_ENV = {
    "VAULT_BASE_URL": "https://vault.test",
    "VAULT_KEY_ID": "11111111-1111-1111-1111-111111111111",
    "BACKEND_TOKEN": "test-token",
    "BACKEND_INSTANCE_ID": "backend-1",
    "REAL_EXCHANGE": "binance",
    "BOT_ID": "22222222-2222-2222-2222-222222222222",
}


def run_script(env: dict) -> subprocess.CompletedProcess:
    base_env = {"PATH": env.pop("PATH", "/usr/bin:/bin"), "HOME": "/tmp"}  # noqa: S108
    base_env.update(env)
    return subprocess.run(
        ["bash", str(START_BOT)],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def make_freqtrade_stub(tmp_path: Path) -> Path:
    """Create a stub `freqtrade` binary so the exec line can be exercised."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "freqtrade"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "FREQTRADE_STUB args: $*"\n'
        'echo "FREQTRADE_STUB pythonpath: ${PYTHONPATH:-}"\n'
        "exit 0\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub_dir


class TestCredentialGuard:
    def test_script_exists_and_is_executable(self):
        assert START_BOT.is_file()
        assert os.access(START_BOT, os.X_OK)

    def test_exchange_api_key_triggers_fatal_exit(self):
        env = dict(REQUIRED_ENV)
        env["EXCHANGE_API_KEY"] = "leaked-key"
        result = run_script(env)
        assert result.returncode == 1
        assert "FATAL" in result.stderr
        assert "EXCHANGE_API_KEY" in result.stderr
        assert "FREQTRADE_STUB" not in result.stdout  # never reached exec

    def test_exchange_api_secret_triggers_fatal_exit(self):
        env = dict(REQUIRED_ENV)
        env["EXCHANGE_API_SECRET"] = "leaked-secret"
        result = run_script(env)
        assert result.returncode == 1
        assert "FATAL" in result.stderr
        assert "EXCHANGE_API_SECRET" in result.stderr

    def test_exchange_api_passphrase_triggers_fatal_exit(self):
        env = dict(REQUIRED_ENV)
        env["EXCHANGE_API_PASSPHRASE"] = "leaked-passphrase"
        result = run_script(env)
        assert result.returncode == 1
        assert "FATAL" in result.stderr
        assert "EXCHANGE_API_PASSPHRASE" in result.stderr

    def test_freqtrade_exchange_env_override_triggers_fatal_exit(self):
        """FREQTRADE__EXCHANGE__* overrides can carry raw credentials."""
        for var in (
            "FREQTRADE__EXCHANGE__KEY",
            "FREQTRADE__EXCHANGE__SECRET",
            "FREQTRADE__EXCHANGE__PASSWORD",
            "FREQTRADE__EXCHANGE__UID",
            "FREQTRADE__EXCHANGE__CCXT_CONFIG__APIKEY",
            "FREQTRADE__EXCHANGE__NAME",
        ):
            env = dict(REQUIRED_ENV)
            env[var] = "leaked-value"
            result = run_script(env)
            assert result.returncode == 1, var
            assert "FATAL" in result.stderr, var
            assert var in result.stderr, var

    def test_non_exchange_freqtrade_overrides_are_allowed(self, tmp_path):
        """Only the exchange section is locked down - other FREQTRADE__
        overrides (e.g. logging) must not trip the guard."""
        stub_dir = make_freqtrade_stub(tmp_path)
        env = dict(REQUIRED_ENV)
        env["FREQTRADE__INTERNALS__PROCESS_THROTTLE_SECS"] = "10"
        env["PATH"] = f"{stub_dir}:/usr/bin:/bin"
        result = run_script(env)
        assert result.returncode == 0, result.stderr
        assert "FATAL" not in result.stderr

    def test_guard_fires_even_with_credentials_and_no_vault_config(self):
        # The guard runs BEFORE required-var checks: a credentialed
        # environment must die with FATAL, not with a missing-var error.
        result = run_script({"EXCHANGE_API_KEY": "leaked-key"})
        assert result.returncode == 1
        assert "FATAL" in result.stderr

    def test_empty_credential_vars_do_not_trigger_guard(self, tmp_path):
        env = dict(REQUIRED_ENV)
        env["EXCHANGE_API_KEY"] = ""
        env["EXCHANGE_API_SECRET"] = ""
        stub_dir = make_freqtrade_stub(tmp_path)
        env["PATH"] = f"{stub_dir}:/usr/bin:/bin"
        result = run_script(env)
        assert result.returncode == 0
        assert "FATAL" not in result.stderr


class TestRequiredVars:
    def test_clean_env_missing_required_vars_fails(self):
        result = run_script({})
        assert result.returncode != 0
        assert "VAULT_BASE_URL" in result.stderr

    def test_each_required_var_is_enforced(self):
        for missing in REQUIRED_ENV:
            env = {k: v for k, v in REQUIRED_ENV.items() if k != missing}
            result = run_script(env)
            assert result.returncode != 0, f"script should fail without {missing}"
            assert missing in result.stderr, f"error should mention {missing}"


class TestLaunch:
    def test_launch_reaches_exec_with_stubbed_freqtrade(self, tmp_path):
        env = dict(REQUIRED_ENV)
        stub_dir = make_freqtrade_stub(tmp_path)
        env["PATH"] = f"{stub_dir}:/usr/bin:/bin"
        result = run_script(env)

        assert result.returncode == 0, result.stderr
        assert "FREQTRADE_STUB args: trade" in result.stdout
        # Config mount path is LOCKED at /freqtrade/config.json (contract C).
        assert "--config /freqtrade/config.json" in result.stdout
        assert "--strategy NerdbotStrategy" in result.stdout

    def test_strategy_override(self, tmp_path):
        env = dict(REQUIRED_ENV)
        env["STRATEGY"] = "MyStrategy"
        stub_dir = make_freqtrade_stub(tmp_path)
        env["PATH"] = f"{stub_dir}:/usr/bin:/bin"
        result = run_script(env)
        assert "--strategy MyStrategy" in result.stdout

    def test_pythonpath_includes_adapter_dir_for_sitecustomize(self, tmp_path):
        env = dict(REQUIRED_ENV)
        stub_dir = make_freqtrade_stub(tmp_path)
        env["PATH"] = f"{stub_dir}:/usr/bin:/bin"
        result = run_script(env)
        assert "user_data/exchange" in result.stdout  # PYTHONPATH echoed by stub

    def test_ai_service_env_vars_pass_the_guard(self, tmp_path):
        """AI_SERVICE_URL/AI_SERVICE_TOKEN/IS_PRO_USER are allowed through
        (they are service config, not exchange credentials) - and the AI
        token is never echoed."""
        env = dict(REQUIRED_ENV)
        env["AI_SERVICE_URL"] = "http://nerdbot-ai:8000"
        env["AI_SERVICE_TOKEN"] = "ai-secret-token"
        env["IS_PRO_USER"] = "true"
        stub_dir = make_freqtrade_stub(tmp_path)
        env["PATH"] = f"{stub_dir}:/usr/bin:/bin"
        result = run_script(env)
        assert result.returncode == 0, result.stderr
        assert "FATAL" not in result.stderr
        assert "ai-secret-token" not in result.stdout
        assert "ai-secret-token" not in result.stderr

    def test_backend_token_not_echoed(self, tmp_path):
        env = dict(REQUIRED_ENV)
        stub_dir = make_freqtrade_stub(tmp_path)
        env["PATH"] = f"{stub_dir}:/usr/bin:/bin"
        result = run_script(env)
        assert REQUIRED_ENV["BACKEND_TOKEN"] not in result.stdout
        assert REQUIRED_ENV["BACKEND_TOKEN"] not in result.stderr
