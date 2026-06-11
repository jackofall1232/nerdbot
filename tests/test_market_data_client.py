"""
Tests for the zero-credential MarketDataClient.

Network-free: delegation tests replace the underlying ccxt instance with a
MagicMock; construction tests assert that NO credentials are ever configured
on the real ccxt object.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from user_data.exchange.market_data_client import MarketDataClient  # noqa: E402


class TestNoCredentials:
    @pytest.mark.parametrize("exchange_name", ["binance", "kraken", "coinbase"])
    def test_ccxt_instance_has_no_credentials(self, exchange_name):
        client = MarketDataClient(exchange_name)
        exchange = client.ccxt_exchange
        assert not exchange.apiKey
        assert not exchange.secret
        assert not getattr(exchange, "password", None)
        assert not getattr(exchange, "uid", None)
        assert not getattr(exchange, "privateKey", None)

    def test_rate_limit_enabled(self):
        client = MarketDataClient("binance")
        assert client.ccxt_exchange.enableRateLimit is True

    def test_credentials_cannot_be_injected_via_constructor(self):
        # The constructor accepts only the exchange name - there is no way to
        # pass credentials at all.
        with pytest.raises(TypeError):
            MarketDataClient("binance", api_key="x")  # type: ignore[call-arg]

    def test_unknown_exchange_rejected(self):
        with pytest.raises(ValueError, match="not supported"):
            MarketDataClient("definitely_not_an_exchange")

    def test_empty_exchange_rejected(self):
        with pytest.raises(ValueError):
            MarketDataClient("")


class TestDelegation:
    @pytest.fixture
    def client(self):
        client = MarketDataClient("binance")
        client._exchange = MagicMock()
        return client

    def test_fetch_ticker(self, client):
        client._exchange.fetch_ticker.return_value = {"last": 100.0}
        assert client.fetch_ticker("SOL/USDT") == {"last": 100.0}
        client._exchange.fetch_ticker.assert_called_once_with("SOL/USDT")

    def test_fetch_ohlcv_defaults(self, client):
        client._exchange.fetch_ohlcv.return_value = [[0, 1, 2, 3, 4, 5]]
        result = client.fetch_ohlcv("SOL/USDT", "5m")
        assert result == [[0, 1, 2, 3, 4, 5]]
        client._exchange.fetch_ohlcv.assert_called_once_with(
            "SOL/USDT", "5m", since=None, limit=500
        )

    def test_fetch_ohlcv_with_since_and_limit(self, client):
        client.fetch_ohlcv("SOL/USDT", "1h", since=1700000000000, limit=42)
        client._exchange.fetch_ohlcv.assert_called_once_with(
            "SOL/USDT", "1h", since=1700000000000, limit=42
        )

    def test_fetch_order_book_default_limit(self, client):
        client._exchange.fetch_order_book.return_value = {"bids": [], "asks": []}
        assert client.fetch_order_book("SOL/USDT") == {"bids": [], "asks": []}
        client._exchange.fetch_order_book.assert_called_once_with("SOL/USDT", limit=20)

    def test_fetch_markets(self, client):
        client._exchange.fetch_markets.return_value = [{"symbol": "SOL/USDT"}]
        assert client.fetch_markets() == [{"symbol": "SOL/USDT"}]
        client._exchange.fetch_markets.assert_called_once_with()
