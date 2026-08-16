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

import logging
import os
from datetime import datetime

import requests

from freqtrade.strategy import timeframe_to_prev_date

# Freqtrade puts this directory on sys.path while loading strategies, so the
# base strategy is importable module-style (documented same-directory import).
from nerdbot_strategy import NerdbotStrategy


logger = logging.getLogger(__name__)

#: Minimum nerdbot-ai score required to confirm a long entry.
AI_SCORE_ENTRY_THRESHOLD = 0.55

#: Hard ceiling on the AI request - trading must never stall on nerdbot-ai.
#: requests bounds connect and read SEPARATELY (read = max gap between
#: bytes), so a (connect, read) tuple is used: 3.05s to connect, 5s read.
AI_REQUEST_TIMEOUT: tuple[float, float] = (3.05, 5.0)


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

        Returns None on ANY failure. SECURITY: the bearer token must never
        appear in logs or propagate inside an exception - only the exception
        CLASS name is ever logged, never its message or headers.
        """
        settings = self._ai_service_settings()
        if settings is None:
            return None
        url, token, exchange, is_pro = settings
        try:
            response = requests.post(
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
            )
            response.raise_for_status()
            score = float(response.json()["score"])
            if not 0.0 <= score <= 1.0:
                raise ValueError("score out of range")
            return score
        except Exception as exc:  # noqa: BLE001 - the AI layer must never raise
            logger.debug(
                "nerdbot-ai call failed for %s (%s) - using standard NerdbotStrategy signals",
                pair,
                type(exc).__name__,
            )
            return None

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
