"""
NerdbotAIStrategy - NerdbotStrategy plus an nerdbot-ai entry filter.

Identical to NerdbotStrategy (same indicators, entries, exits, ROI,
stoploss), with one addition: right before a long entry order is placed,
the nerdbot-ai microservice is asked for a signal score and the entry is
only confirmed when the score is at least ``AI_SCORE_ENTRY_THRESHOLD``.

The AI layer only ever ADJUSTS the base strategy - it must never block
trading outright. Therefore ANY failure (env not configured, HTTP error,
timeout, malformed payload, score out of range) silently degrades to plain
NerdbotStrategy behavior: the entry is allowed exactly as the base strategy
decided. Failures are logged once at debug level, and the service token
never appears in logs or exceptions.

nerdbot-ai contract (POST {AI_SERVICE_URL}/features, bearer auth):
    request : {"pair": str, "timeframe": str, "exchange": str, "is_pro": bool}
    response: {"pair": str, "timeframe": str, "score": float, "tier": str}
    with score in [0, 1].

Environment (set by the backend at container start; all optional - when
incomplete the AI layer is simply disabled):
    AI_SERVICE_URL    base URL of the nerdbot-ai service
    AI_SERVICE_TOKEN  bearer token for the service (NEVER logged)
    IS_PRO_USER       "true"/"false" - passed through as is_pro
    REAL_EXCHANGE     underlying exchange id (binance|kraken|coinbase)

The score is cached per (pair, latest-candle-timestamp) - failures
included - so each pair triggers at most one HTTP call per candle.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime

import requests

# Freqtrade puts this directory on sys.path while loading strategies, so the
# base strategy is importable module-style (documented same-directory import).
from nerdbot_strategy import NerdbotStrategy

from freqtrade.strategy import timeframe_to_prev_date


logger = logging.getLogger(__name__)

#: Minimum nerdbot-ai score required to confirm a long entry.
AI_SCORE_ENTRY_THRESHOLD = 0.55

#: Hard ceiling on the AI request - trading must never stall on nerdbot-ai.
#: requests bounds connect and read SEPARATELY (read = max gap between
#: bytes), so a (connect, read) tuple is used: 3.05s to connect, 5s read.
AI_REQUEST_TIMEOUT: tuple[float, float] = (3.05, 5.0)

#: The read timeout above only caps the gap BETWEEN bytes - a server that
#: drips one byte every few seconds would never trip it. Two further layers
#: therefore bound the call: the body is streamed against this wall-clock
#: deadline (and a size cap far above any real /features payload), and the
#: WHOLE request runs on a dedicated worker thread that the trading loop
#: waits on for at most AI_TOTAL_DEADLINE_SECONDS - even a server dripping
#: response HEADERS (which no requests timeout fully bounds) can only stall
#: the worker, never confirm_trade_entry.
AI_TOTAL_DEADLINE_SECONDS = 6.0
AI_MAX_RESPONSE_BYTES = 65536

#: After a timeout-class failure the AI layer is skipped for ALL pairs
#: for this long: without it, a stalled service costs every uncached
#: pair in the same entry pass its own full deadline (50 pairs -> 5+
#: minutes of loop delay despite each request being individually capped).
AI_FAILURE_BACKOFF_SECONDS = 30.0


class NerdbotAIStrategy(NerdbotStrategy):
    """NerdbotStrategy with an nerdbot-ai score gate on long entries."""

    INTERFACE_VERSION = 3

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        # pair -> (candle timestamp, score or None). None records a failed
        # or unavailable lookup so a broken service is still asked at most
        # once per candle.
        self._ai_score_cache: dict[str, tuple[datetime, float | None]] = {}
        self._ai_disabled_logged = False
        # One session per bot lifetime: reuses the TCP/TLS connection to
        # nerdbot-ai instead of a fresh handshake every candle.
        self._ai_session = requests.Session()
        # The request runs on a short-lived DAEMON thread the trading loop
        # waits on for at most the total deadline. Daemon threads are never
        # joined at interpreter shutdown, so a wedged request can never
        # block bot stop (a ThreadPoolExecutor worker would be). While a
        # wedged thread is still alive, AI is skipped (degrade), not queued.
        self._ai_inflight_thread: threading.Thread | None = None
        # monotonic timestamp until which AI lookups are skipped entirely
        # (set after timeout-class failures - see AI_FAILURE_BACKOFF_SECONDS).
        self._ai_backoff_until = 0.0

    # ------------------------------------------------------------------
    # nerdbot-ai client (private helpers)
    # ------------------------------------------------------------------

    def _ai_service_settings(self) -> tuple[str, str, str, bool] | None:
        """
        Read the nerdbot-ai configuration from the environment.

        Returns (url, token, exchange, is_pro), or None when the service is
        not (fully) configured - in which case the AI layer is disabled and
        the strategy behaves exactly like NerdbotStrategy.
        """
        url = os.environ.get("AI_SERVICE_URL", "").strip().rstrip("/")
        token = os.environ.get("AI_SERVICE_TOKEN", "").strip()
        exchange = os.environ.get("REAL_EXCHANGE", "").strip().lower()
        is_pro = os.environ.get("IS_PRO_USER", "false").strip().lower() == "true"
        if not url or not token or not exchange:
            if not self._ai_disabled_logged:
                # Log once per bot lifetime - this is a static deployment
                # property, not a per-candle event.
                logger.debug(
                    "nerdbot-ai not configured (AI_SERVICE_URL/AI_SERVICE_TOKEN/"
                    "REAL_EXCHANGE incomplete) - using standard NerdbotStrategy signals"
                )
                self._ai_disabled_logged = True
            return None
        return url, token, exchange, is_pro

    def _fetch_ai_score(self, pair: str) -> float | None:
        """
        Call POST {AI_SERVICE_URL}/features and return the score in [0, 1].

        Returns None on ANY failure. The blocking request runs on the
        dedicated worker thread and is abandoned (not awaited) once the
        total deadline passes, so the trading loop's wait is hard-bounded.
        SECURITY: the bearer token must never appear in logs or propagate
        inside an exception - only the exception CLASS name is ever logged,
        never its message or headers.
        """
        settings = self._ai_service_settings()
        if settings is None:
            return None
        url, token, exchange, is_pro = settings
        if time.monotonic() < self._ai_backoff_until:
            # A recent timeout-class failure: shed load for every pair
            # instead of paying the full deadline once per pair.
            return None
        failure_name = "Error"
        try:
            inflight = self._ai_inflight_thread
            if inflight is not None and inflight.is_alive():
                # A previous request is still wedged - skip AI entirely
                # rather than stacking threads behind it.
                raise TimeoutError("previous AI request still in flight")
            self._ai_inflight_thread = None

            # SECURITY: only the failure's CLASS NAME crosses the thread
            # boundary - never the exception object (its message could
            # embed header/token material).
            outcome: dict[str, object] = {}
            done = threading.Event()

            def _worker() -> None:
                try:
                    outcome["score"] = self._fetch_ai_score_blocking(
                        pair, url, token, exchange, is_pro
                    )
                except BaseException as exc:
                    outcome["failure"] = type(exc).__name__
                finally:
                    done.set()

            thread = threading.Thread(target=_worker, name="nerdbot-ai", daemon=True)
            thread.start()
            # Small grace over the in-request deadline so the worker's own
            # (tighter) limits normally fire first.
            if not done.wait(AI_TOTAL_DEADLINE_SECONDS + 0.5):
                self._ai_inflight_thread = thread
                raise TimeoutError("AI request exceeded total deadline")
            score = outcome.get("score")
            if isinstance(score, float):
                return score
            failure_name = str(outcome.get("failure", "Error"))
            raise RuntimeError("AI request failed")
        except Exception as exc:
            reported = failure_name if type(exc).__name__ == "RuntimeError" else type(exc).__name__
            if isinstance(exc, TimeoutError) or "Timeout" in reported:
                # Slow-failure class: back off globally so one stalled
                # service costs a single deadline per backoff window, not
                # one per uncached pair.
                self._ai_backoff_until = time.monotonic() + AI_FAILURE_BACKOFF_SECONDS
            logger.debug(
                "nerdbot-ai call failed for %s (%s) - using standard NerdbotStrategy signals",
                pair,
                reported,
            )
            return None

    def _fetch_ai_score_blocking(
        self, pair: str, url: str, token: str, exchange: str, is_pro: bool
    ) -> float:
        """Worker-thread body: the actual HTTP call. Raises on any failure."""
        response = self._ai_session.post(
            f"{url}/features",
            json={
                "pair": pair,
                "timeframe": self.timeframe,
                "exchange": exchange,
                "is_pro": is_pro,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=AI_REQUEST_TIMEOUT,
            # The service never redirects; following one could resend
            # the bearer token to an unexpected host.
            allow_redirects=False,
            # Streamed so the body read below can enforce a TOTAL
            # wall-clock deadline (the read timeout is only per-gap).
            stream=True,
        )
        try:
            response.raise_for_status()
            deadline = time.monotonic() + AI_TOTAL_DEADLINE_SECONDS
            body = bytearray()
            for chunk in response.iter_content(chunk_size=4096):
                body.extend(chunk)
                if time.monotonic() > deadline:
                    raise TimeoutError("AI response exceeded total deadline")
                if len(body) > AI_MAX_RESPONSE_BYTES:
                    raise ValueError("AI response too large")
        finally:
            response.close()
        payload = json.loads(bytes(body))
        # A stale or misrouted response for another market must never
        # gate this pair's entry - treat it as malformed.
        if payload.get("pair") != pair or payload.get("timeframe") != self.timeframe:
            raise ValueError("AI response identity mismatch")
        score_raw = payload["score"]
        # bool is an int subclass: float(False) == 0.0 would VETO instead
        # of degrading - a boolean score is malformed, not a decision.
        if isinstance(score_raw, bool) or not isinstance(score_raw, (int, float)):
            raise ValueError("AI score is not numeric")
        score = float(score_raw)
        if not 0.0 <= score <= 1.0:
            raise ValueError("score out of range")
        return score

    def _ai_score(self, pair: str, current_time: datetime) -> float | None:
        """
        Score for the latest candle, cached per (pair, candle timestamp).

        Failures are cached as None so each candle triggers at most one
        HTTP call per pair, even while the service is down.
        """
        candle_ts = timeframe_to_prev_date(self.timeframe, current_time)
        cached = self._ai_score_cache.get(pair)
        if cached is not None and cached[0] == candle_ts:
            return cached[1]
        score = self._fetch_ai_score(pair)
        self._ai_score_cache[pair] = (candle_ts, score)
        return score

    # ------------------------------------------------------------------
    # Strategy callbacks
    # ------------------------------------------------------------------

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> bool:
        """
        Gate long entries on the nerdbot-ai score.

        score >= AI_SCORE_ENTRY_THRESHOLD confirms the entry; a lower score
        vetoes it. No score (service unconfigured / failing) falls back to
        the base strategy's decision - the AI layer adjusts, never blocks.
        """
        base_decision = super().confirm_trade_entry(
            pair=pair,
            order_type=order_type,
            amount=amount,
            rate=rate,
            time_in_force=time_in_force,
            current_time=current_time,
            entry_tag=entry_tag,
            side=side,
            **kwargs,
        )
        if side != "long" or not base_decision:
            return base_decision

        score = self._ai_score(pair, current_time)
        if score is None:
            # Degraded mode: behave exactly like NerdbotStrategy.
            return base_decision
        return score >= AI_SCORE_ENTRY_THRESHOLD
