"""
Tests for the Nerdbot strategies (user_data/strategies/).

- NerdbotStrategy: indicator computation and the exact entry/exit rules
  (verified on hand-crafted indicator frames - deterministic, network-free).
- NerdbotAIStrategy: the nerdbot-ai entry gate, with `requests` mocked -
  success path, every failure path (env unset, HTTP error, timeout, bad
  payload), per-(pair, candle) caching, and token-never-logged.
- Both strategies resolve through freqtrade's StrategyResolver, proving the
  launched bot can actually load them.
"""

import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
import requests as requests_lib


REPO_ROOT = Path(__file__).resolve().parents[1]
STRATEGY_DIR = REPO_ROOT / "user_data" / "strategies"
for path in (str(REPO_ROOT), str(STRATEGY_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import nerdbot_ai_strategy as ai_module  # noqa: E402
from nerdbot_ai_strategy import AI_SCORE_ENTRY_THRESHOLD, NerdbotAIStrategy  # noqa: E402
from nerdbot_strategy import NerdbotStrategy  # noqa: E402


TOKEN = "super-secret-ai-token"
CANDLE_TIME = datetime(2026, 8, 16, 12, 3, 27, tzinfo=UTC)


def make_strategy(cls):
    return cls({"strategy": cls.__name__})


def make_ohlcv(rows: int = 120, seed: int = 42) -> pd.DataFrame:
    """Synthetic OHLCV random-walk frame (deterministic)."""
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0, 0.5, rows))
    high = close + rng.uniform(0.05, 0.5, rows)
    low = close - rng.uniform(0.05, 0.5, rows)
    open_ = close + rng.normal(0, 0.2, rows)
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-08-01", periods=rows, freq="5min", tz="UTC"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(1.0, 100.0, rows),
        }
    )


# =============================================================================
# NerdbotStrategy: contract-level attributes
# =============================================================================


class TestNerdbotStrategyAttributes:
    def test_interface_version_3(self):
        assert NerdbotStrategy.INTERFACE_VERSION == 3

    def test_timeframe_is_5m(self):
        assert NerdbotStrategy.timeframe == "5m"

    def test_long_only(self):
        assert NerdbotStrategy.can_short is False

    def test_conservative_stoploss(self):
        assert NerdbotStrategy.stoploss == -0.05

    def test_conservative_roi_ladder(self):
        roi = NerdbotStrategy.minimal_roi
        assert roi["0"] == 0.03
        # ROI targets shrink as the trade ages, ending at break-even.
        values = [roi[k] for k in sorted(roi, key=int)]
        assert values == sorted(values, reverse=True)
        assert values[-1] == 0.0

    def test_no_stoploss_on_exchange(self):
        # Orders route through the vault/dry-run - never native stop orders.
        assert NerdbotStrategy.order_types["stoploss_on_exchange"] is False

    def test_startup_candle_count_covers_macd_warmup(self):
        assert 35 <= NerdbotStrategy.startup_candle_count <= 200

    def test_process_only_new_candles(self):
        assert NerdbotStrategy.process_only_new_candles is True


# =============================================================================
# NerdbotStrategy: indicators
# =============================================================================


class TestNerdbotStrategyIndicators:
    def test_populate_indicators_adds_expected_columns(self):
        strategy = make_strategy(NerdbotStrategy)
        df = strategy.populate_indicators(make_ohlcv(), {"pair": "SOL/USD"})
        for column in (
            "rsi",
            "macd",
            "macdsignal",
            "macdhist",
            "bb_lowerband",
            "bb_middleband",
            "bb_upperband",
        ):
            assert column in df.columns, column
            # After warm-up the indicator must produce real values.
            assert df[column].iloc[-1] == df[column].iloc[-1], f"{column} is NaN at tail"

    def test_indicators_are_sane(self):
        strategy = make_strategy(NerdbotStrategy)
        df = strategy.populate_indicators(make_ohlcv(), {"pair": "SOL/USD"})
        tail = df.iloc[50:]
        assert tail["rsi"].between(0, 100).all()
        assert (tail["bb_lowerband"] <= tail["bb_middleband"]).all()
        assert (tail["bb_middleband"] <= tail["bb_upperband"]).all()

    def test_full_pipeline_produces_signal_columns_without_errors(self):
        strategy = make_strategy(NerdbotStrategy)
        metadata = {"pair": "SOL/USD"}
        df = strategy.populate_indicators(make_ohlcv(rows=300), metadata)
        df = strategy.populate_entry_trend(df, metadata)
        df = strategy.populate_exit_trend(df, metadata)
        assert "enter_long" in df.columns
        assert "exit_long" in df.columns
        # Long-only strategy: no short columns are ever set.
        assert "enter_short" not in df.columns
        assert set(df["enter_long"].dropna().unique()) <= {1}
        assert set(df["exit_long"].dropna().unique()) <= {1}


# =============================================================================
# NerdbotStrategy: exact entry/exit rules (hand-crafted indicator frames)
# =============================================================================


def entry_frame(**overrides) -> pd.DataFrame:
    """
    Two-candle frame whose last row satisfies every entry condition:
    RSI crosses above 30, close below BB middle, MACD above signal, volume>0.
    """
    frame = {
        "rsi": [28.0, 32.0],  # crossed above 30
        "close": [99.0, 99.0],
        "bb_middleband": [100.0, 100.0],  # close < mid
        "bb_upperband": [102.0, 102.0],
        "macd": [0.5, 0.5],
        "macdsignal": [0.1, 0.1],  # macd > signal
        "volume": [10.0, 10.0],
    }
    frame.update(overrides)
    return pd.DataFrame(frame)


class TestEntryRule:
    def make(self):
        return make_strategy(NerdbotStrategy)

    def test_fires_on_oversold_recovery(self):
        df = self.make().populate_entry_trend(entry_frame(), {"pair": "SOL/USD"})
        assert df["enter_long"].iloc[-1] == 1

    def test_requires_rsi_cross_not_level(self):
        # RSI already above 30 on both candles: no cross, no entry.
        df = self.make().populate_entry_trend(entry_frame(rsi=[35.0, 36.0]), {"pair": "SOL/USD"})
        assert "enter_long" not in df.columns or df["enter_long"].iloc[-1] != 1

    def test_blocked_above_bb_middleband(self):
        df = self.make().populate_entry_trend(
            entry_frame(close=[101.0, 101.0]), {"pair": "SOL/USD"}
        )
        assert "enter_long" not in df.columns or df["enter_long"].iloc[-1] != 1

    def test_blocked_when_macd_bearish(self):
        df = self.make().populate_entry_trend(
            entry_frame(macd=[0.0, 0.0], macdsignal=[0.5, 0.5]), {"pair": "SOL/USD"}
        )
        assert "enter_long" not in df.columns or df["enter_long"].iloc[-1] != 1

    def test_blocked_on_zero_volume(self):
        df = self.make().populate_entry_trend(entry_frame(volume=[10.0, 0.0]), {"pair": "SOL/USD"})
        assert "enter_long" not in df.columns or df["enter_long"].iloc[-1] != 1


class TestExitRule:
    def make(self):
        return make_strategy(NerdbotStrategy)

    def test_fires_on_rsi_overbought_cross(self):
        df = self.make().populate_exit_trend(entry_frame(rsi=[68.0, 72.0]), {"pair": "SOL/USD"})
        assert df["exit_long"].iloc[-1] == 1

    def test_fires_on_upper_band_breakout(self):
        df = self.make().populate_exit_trend(entry_frame(close=[101.0, 103.0]), {"pair": "SOL/USD"})
        assert df["exit_long"].iloc[-1] == 1

    def test_no_exit_without_signal(self):
        df = self.make().populate_exit_trend(entry_frame(), {"pair": "SOL/USD"})
        assert "exit_long" not in df.columns or df["exit_long"].iloc[-1] != 1

    def test_exit_blocked_on_zero_volume(self):
        df = self.make().populate_exit_trend(
            entry_frame(rsi=[68.0, 72.0], volume=[10.0, 0.0]), {"pair": "SOL/USD"}
        )
        assert "exit_long" not in df.columns or df["exit_long"].iloc[-1] != 1


# =============================================================================
# NerdbotAIStrategy: nerdbot-ai entry gate
# =============================================================================


@pytest.fixture
def ai_env(monkeypatch):
    monkeypatch.setenv("AI_SERVICE_URL", "http://nerdbot-ai:8000")
    monkeypatch.setenv("AI_SERVICE_TOKEN", TOKEN)
    monkeypatch.setenv("REAL_EXCHANGE", "kraken")
    monkeypatch.setenv("IS_PRO_USER", "true")


@pytest.fixture
def no_ai_env(monkeypatch):
    for var in ("AI_SERVICE_URL", "AI_SERVICE_TOKEN", "REAL_EXCHANGE", "IS_PRO_USER"):
        monkeypatch.delenv(var, raising=False)


def make_ai_strategy():
    return make_strategy(NerdbotAIStrategy)


def make_response(score, payload=None, body=None):
    """Mock of a streamed requests.Response (raise_for_status/iter_content/close)."""
    response = MagicMock()
    response.raise_for_status.return_value = None
    if body is None:
        data = (
            payload
            if payload is not None
            else {"pair": "SOL/USD", "timeframe": "5m", "score": score, "tier": "enhanced"}
        )
        body = json.dumps(data).encode()
    # Fresh iterator per call so a cached response mock can be re-read.
    response.iter_content.side_effect = lambda chunk_size=4096: iter([body])
    return response


def mock_post(monkeypatch, response=None, side_effect=None):
    post = MagicMock()
    if side_effect is not None:
        post.side_effect = side_effect
    else:
        post.return_value = response if response is not None else make_response(0.9)
    # The strategy posts through a requests.Session created in __init__;
    # patching at class level covers every instance regardless of when it
    # was constructed.
    monkeypatch.setattr(ai_module.requests.Session, "post", post)
    return post


def confirm(strategy, pair="SOL/USD", side="long", current_time=CANDLE_TIME):
    return strategy.confirm_trade_entry(
        pair=pair,
        order_type="limit",
        amount=1.0,
        rate=100.0,
        time_in_force="GTC",
        current_time=current_time,
        entry_tag=None,
        side=side,
    )


class TestAIStrategyInheritance:
    def test_subclasses_nerdbot_strategy(self):
        assert issubclass(NerdbotAIStrategy, NerdbotStrategy)
        assert NerdbotAIStrategy.INTERFACE_VERSION == 3

    def test_base_signals_are_not_overridden(self):
        # The AI layer only ever gates entries - the signal logic itself
        # must be untouched NerdbotStrategy code.
        for method in ("populate_indicators", "populate_entry_trend", "populate_exit_trend"):
            assert method not in NerdbotAIStrategy.__dict__, method


class TestAISuccessPath:
    def test_high_score_confirms_entry(self, ai_env, monkeypatch):
        post = mock_post(monkeypatch, make_response(0.9))
        assert confirm(make_ai_strategy()) is True
        post.assert_called_once()

    def test_low_score_vetoes_entry(self, ai_env, monkeypatch):
        mock_post(monkeypatch, make_response(0.2))
        assert confirm(make_ai_strategy()) is False

    def test_threshold_is_inclusive(self, ai_env, monkeypatch):
        mock_post(monkeypatch, make_response(AI_SCORE_ENTRY_THRESHOLD))
        assert confirm(make_ai_strategy()) is True

    def test_just_below_threshold_vetoes(self, ai_env, monkeypatch):
        mock_post(monkeypatch, make_response(AI_SCORE_ENTRY_THRESHOLD - 0.01))
        assert confirm(make_ai_strategy()) is False

    def test_request_matches_nerdbot_ai_contract(self, ai_env, monkeypatch):
        post = mock_post(monkeypatch)
        confirm(make_ai_strategy(), pair="BTC/USD")

        args, kwargs = post.call_args
        assert args == ("http://nerdbot-ai:8000/features",)
        assert kwargs["json"] == {
            "pair": "BTC/USD",
            "timeframe": "5m",
            "exchange": "kraken",
            "is_pro": True,
        }
        assert kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}
        # (connect, read) tuple - each component bounded by the 5s contract
        # ceiling; requests treats a scalar as the between-bytes read gap,
        # which would not cap wall time.
        connect_timeout, read_timeout = kwargs["timeout"]
        assert connect_timeout <= 5.0
        assert read_timeout <= 5.0
        # Redirects must never be followed (would re-send the bearer token).
        assert kwargs["allow_redirects"] is False
        # Streamed so the body read enforces a total wall-clock deadline.
        assert kwargs["stream"] is True

    def test_is_pro_false_by_default(self, ai_env, monkeypatch):
        monkeypatch.delenv("IS_PRO_USER")
        post = mock_post(monkeypatch)
        confirm(make_ai_strategy())
        assert post.call_args.kwargs["json"]["is_pro"] is False

    def test_is_pro_false_when_not_true(self, ai_env, monkeypatch):
        monkeypatch.setenv("IS_PRO_USER", "false")
        post = mock_post(monkeypatch)
        confirm(make_ai_strategy())
        assert post.call_args.kwargs["json"]["is_pro"] is False

    def test_trailing_slash_on_url_is_normalized(self, ai_env, monkeypatch):
        monkeypatch.setenv("AI_SERVICE_URL", "http://nerdbot-ai:8000/")
        post = mock_post(monkeypatch)
        confirm(make_ai_strategy())
        assert post.call_args.args[0] == "http://nerdbot-ai:8000/features"

    def test_short_side_never_calls_ai(self, ai_env, monkeypatch):
        post = mock_post(monkeypatch)
        assert confirm(make_ai_strategy(), side="short") is True
        post.assert_not_called()


class TestAIFailurePaths:
    """ANY failure must silently degrade to NerdbotStrategy behavior (allow)."""

    def test_env_unset_allows_entry_without_http_call(self, no_ai_env, monkeypatch):
        post = mock_post(monkeypatch)
        assert confirm(make_ai_strategy()) is True
        post.assert_not_called()

    @pytest.mark.parametrize("missing", ["AI_SERVICE_URL", "AI_SERVICE_TOKEN", "REAL_EXCHANGE"])
    def test_each_missing_env_var_disables_ai(self, ai_env, monkeypatch, missing):
        monkeypatch.delenv(missing)
        post = mock_post(monkeypatch)
        assert confirm(make_ai_strategy()) is True
        post.assert_not_called()

    def test_http_error_allows_entry(self, ai_env, monkeypatch):
        response = make_response(0.9)
        response.raise_for_status.side_effect = requests_lib.exceptions.HTTPError("401")
        mock_post(monkeypatch, response)
        assert confirm(make_ai_strategy()) is True

    def test_timeout_allows_entry(self, ai_env, monkeypatch):
        mock_post(monkeypatch, side_effect=requests_lib.exceptions.Timeout("slow"))
        assert confirm(make_ai_strategy()) is True

    def test_connection_error_allows_entry(self, ai_env, monkeypatch):
        mock_post(monkeypatch, side_effect=requests_lib.exceptions.ConnectionError("down"))
        assert confirm(make_ai_strategy()) is True

    @pytest.mark.parametrize(
        "payload",
        [
            {},  # score missing entirely
            {"score": "not-a-number"},
            {"score": None},
            {"score": 1.5},  # out of [0, 1]
            {"score": -0.2},
            ["not", "a", "dict"],
        ],
    )
    def test_bad_payload_allows_entry(self, ai_env, monkeypatch, payload):
        mock_post(monkeypatch, make_response(None, payload=payload))
        assert confirm(make_ai_strategy()) is True

    def test_non_json_body_allows_entry(self, ai_env, monkeypatch):
        mock_post(monkeypatch, make_response(0.9, body=b"<html>not json</html>"))
        assert confirm(make_ai_strategy()) is True

    @pytest.mark.parametrize(
        "payload",
        [
            # A response labeled for another pair or timeframe must never
            # gate this pair's entry (stale/misrouted response).
            {"pair": "BTC/USD", "timeframe": "5m", "score": 0.1, "tier": "enhanced"},
            {"pair": "SOL/USD", "timeframe": "1h", "score": 0.1, "tier": "enhanced"},
        ],
    )
    def test_identity_mismatch_allows_entry(self, ai_env, monkeypatch, payload):
        mock_post(monkeypatch, make_response(None, payload=payload))
        assert confirm(make_ai_strategy(), pair="SOL/USD") is True

    def test_oversized_response_allows_entry(self, ai_env, monkeypatch):
        big = b'{"pad": "' + b"x" * (ai_module.AI_MAX_RESPONSE_BYTES + 1) + b'"}'
        mock_post(monkeypatch, make_response(None, body=big))
        assert confirm(make_ai_strategy()) is True

    def test_slow_drip_response_hits_total_deadline(self, ai_env, monkeypatch):
        # Each inter-chunk gap can stay under the read timeout while total
        # wall time grows without bound - the monotonic deadline must cut
        # the read off and degrade to the base decision.
        clock = iter([0.0, ai_module.AI_TOTAL_DEADLINE_SECONDS + 1.0])
        monkeypatch.setattr(ai_module.time, "monotonic", lambda: next(clock))
        response = make_response(0.9)
        response.iter_content.side_effect = lambda chunk_size=4096: iter([b"{", b"}"])
        mock_post(monkeypatch, response)
        assert confirm(make_ai_strategy()) is True

    def test_failure_logged_at_debug_only(self, ai_env, monkeypatch, caplog):
        mock_post(monkeypatch, side_effect=requests_lib.exceptions.Timeout("slow"))
        with caplog.at_level(logging.DEBUG, logger=ai_module.__name__):
            confirm(make_ai_strategy())
        ai_records = [r for r in caplog.records if r.name == ai_module.__name__]
        assert ai_records, "expected a debug log for the failed AI call"
        assert all(r.levelno == logging.DEBUG for r in ai_records)


class TestAIScoreCache:
    def test_same_candle_triggers_single_http_call(self, ai_env, monkeypatch):
        post = mock_post(monkeypatch)
        strategy = make_ai_strategy()
        assert confirm(strategy) is True
        # Later within the SAME candle (12:00 for both timestamps).
        assert confirm(strategy, current_time=CANDLE_TIME + timedelta(seconds=30)) is True
        post.assert_called_once()

    def test_new_candle_triggers_new_call(self, ai_env, monkeypatch):
        post = mock_post(monkeypatch)
        strategy = make_ai_strategy()
        confirm(strategy)
        confirm(strategy, current_time=CANDLE_TIME + timedelta(minutes=5))
        assert post.call_count == 2

    def test_cache_is_per_pair(self, ai_env, monkeypatch):
        post = mock_post(monkeypatch)
        strategy = make_ai_strategy()
        confirm(strategy, pair="SOL/USD")
        confirm(strategy, pair="BTC/USD")
        assert post.call_count == 2

    def test_failures_are_cached_per_candle(self, ai_env, monkeypatch):
        post = mock_post(monkeypatch, side_effect=requests_lib.exceptions.Timeout("slow"))
        strategy = make_ai_strategy()
        assert confirm(strategy) is True
        assert confirm(strategy, current_time=CANDLE_TIME + timedelta(seconds=45)) is True
        post.assert_called_once()  # the failure was cached for this candle

    def test_failure_is_retried_on_next_candle(self, ai_env, monkeypatch):
        post = mock_post(monkeypatch, side_effect=requests_lib.exceptions.Timeout("slow"))
        strategy = make_ai_strategy()
        confirm(strategy)
        confirm(strategy, current_time=CANDLE_TIME + timedelta(minutes=5))
        assert post.call_count == 2


class TestAITokenNeverLeaks:
    def test_token_not_logged_on_success(self, ai_env, monkeypatch, caplog):
        mock_post(monkeypatch)
        with caplog.at_level(logging.DEBUG):
            confirm(make_ai_strategy())
        assert TOKEN not in caplog.text

    def test_token_not_logged_on_failure(self, ai_env, monkeypatch, caplog):
        # The exception message deliberately CONTAINS the token: the strategy
        # must log only the exception class, never its message.
        mock_post(
            monkeypatch,
            side_effect=requests_lib.exceptions.HTTPError(f"401 Bearer {TOKEN} rejected"),
        )
        with caplog.at_level(logging.DEBUG):
            assert confirm(make_ai_strategy()) is True
        assert TOKEN not in caplog.text
        for record in caplog.records:
            assert TOKEN not in record.getMessage()

    def test_token_never_in_raised_exceptions(self, ai_env, monkeypatch):
        # confirm_trade_entry must never raise at all.
        mock_post(monkeypatch, side_effect=RuntimeError(f"boom {TOKEN}"))
        assert confirm(make_ai_strategy()) is True


# =============================================================================
# Both strategies resolve through freqtrade's StrategyResolver
# =============================================================================


class TestStrategyResolution:
    @pytest.mark.parametrize("name", ["NerdbotStrategy", "NerdbotAIStrategy"])
    def test_resolver_loads_strategy(self, name):
        from freqtrade.resolvers import StrategyResolver

        config = {
            "strategy": name,
            "user_data_dir": REPO_ROOT / "user_data",
            "strategy_path": STRATEGY_DIR,
        }
        strategy = StrategyResolver.load_strategy(config)
        assert type(strategy).__name__ == name
        assert strategy.timeframe == "5m"
        assert strategy.can_short is False
