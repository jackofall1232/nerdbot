"""
Read-only CCXT market-data client.

This client talks DIRECTLY to the real exchange's PUBLIC endpoints via ccxt.
It is constructed with NO credentials whatsoever (no apiKey, no secret, no
password) so it is physically incapable of performing authenticated calls.

All credentialed operations go through the vault proxy instead
(see vault_http_client.py / nerdbot_vault.py).
"""

import logging

import ccxt


logger = logging.getLogger(__name__)


class MarketDataClient:
    """
    Zero-credential ccxt wrapper for public market data.

    :param exchange_name: ccxt exchange id, e.g. "binance", "kraken",
                          "coinbase".
    """

    def __init__(self, exchange_name: str) -> None:
        if not exchange_name:
            raise ValueError("exchange_name is required")
        exchange_name = exchange_name.lower().strip()
        try:
            exchange_class = getattr(ccxt, exchange_name)
        except AttributeError as exc:
            raise ValueError(f"Exchange '{exchange_name}' is not supported by ccxt") from exc

        # SECURITY: no apiKey / secret / password are EVER passed here.
        self._exchange: ccxt.Exchange = exchange_class({"enableRateLimit": True})
        self.exchange_name = exchange_name

    @property
    def ccxt_exchange(self) -> ccxt.Exchange:
        """The underlying (credential-free) ccxt instance."""
        return self._exchange

    def fetch_ticker(self, symbol: str) -> dict:
        """Fetch the current ticker for a symbol (public endpoint)."""
        return self._exchange.fetch_ticker(symbol)

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int | None = None,
        limit: int = 500,
    ) -> list:
        """Fetch OHLCV candles (public endpoint)."""
        return self._exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)

    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        """Fetch the order book (public endpoint)."""
        return self._exchange.fetch_order_book(symbol, limit=limit)

    def fetch_markets(self) -> list:
        """Fetch market definitions (public endpoint)."""
        return self._exchange.fetch_markets()
