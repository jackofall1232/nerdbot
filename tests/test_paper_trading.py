"""
Tests for the in-memory PaperTradingSimulator.

Network-free: the market data client is a MagicMock returning a fixed
"real" market price.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import ccxt
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from user_data.exchange.paper_trading import PaperTradingSimulator  # noqa: E402


MARKET_PRICE = 100.0
FEE = 0.001


@pytest.fixture
def market_client():
    client = MagicMock()
    client.fetch_ticker.return_value = {"last": MARKET_PRICE}
    return client


@pytest.fixture
def simulator(market_client):
    return PaperTradingSimulator({"USDT": 1000.0}, market_client)


class TestMarketOrders:
    def test_market_buy_fills_at_real_market_price(self, simulator, market_client):
        order = simulator.simulate_order("SOL/USDT", "market", "buy", 2.0)

        market_client.fetch_ticker.assert_called_once_with("SOL/USDT")
        assert order["status"] == "closed"
        assert order["average"] == MARKET_PRICE
        assert order["filled"] == 2.0
        assert order["remaining"] == 0.0
        assert order["cost"] == pytest.approx(200.0)
        assert order["fee"]["cost"] == pytest.approx(200.0 * FEE)
        assert order["fee"]["currency"] == "USDT"

    def test_market_buy_balance_accounting(self, simulator):
        simulator.simulate_order("SOL/USDT", "market", "buy", 2.0)
        balance = simulator.get_balance()
        # 1000 - 200 cost - 0.2 fee
        assert balance["USDT"]["free"] == pytest.approx(799.8)
        assert balance["SOL"]["free"] == pytest.approx(2.0)
        assert balance["SOL"]["total"] == pytest.approx(2.0)

    def test_round_trip_buy_then_sell(self, simulator):
        simulator.simulate_order("SOL/USDT", "market", "buy", 2.0)
        order = simulator.simulate_order("SOL/USDT", "market", "sell", 2.0)
        assert order["status"] == "closed"
        balance = simulator.get_balance()
        assert balance["SOL"]["free"] == pytest.approx(0.0)
        # 799.8 + 200 proceeds - 0.2 sell fee
        assert balance["USDT"]["free"] == pytest.approx(999.6)

    def test_insufficient_quote_funds(self, simulator):
        with pytest.raises(ccxt.InsufficientFunds):
            simulator.simulate_order("SOL/USDT", "market", "buy", 100.0)  # cost 10000

    def test_insufficient_base_funds_on_sell(self, simulator):
        with pytest.raises(ccxt.InsufficientFunds):
            simulator.simulate_order("SOL/USDT", "market", "sell", 1.0)


class TestLimitOrders:
    def test_marketable_limit_buy_fills_at_limit_price(self, simulator):
        order = simulator.simulate_order("SOL/USDT", "limit", "buy", 1.0, 110.0)
        assert order["status"] == "closed"
        assert order["average"] == 110.0

    def test_marketable_limit_sell_fills(self, simulator):
        simulator.simulate_order("SOL/USDT", "market", "buy", 2.0)
        order = simulator.simulate_order("SOL/USDT", "limit", "sell", 1.0, 90.0)
        assert order["status"] == "closed"
        assert order["average"] == 90.0

    def test_non_marketable_limit_buy_rests_open_and_reserves_funds(self, simulator):
        order = simulator.simulate_order("SOL/USDT", "limit", "buy", 1.0, 90.0)
        assert order["status"] == "open"
        assert order["filled"] == 0.0
        assert order["remaining"] == 1.0

        balance = simulator.get_balance()
        reserved = 90.0 * (1 + FEE)
        assert balance["USDT"]["used"] == pytest.approx(reserved)
        assert balance["USDT"]["free"] == pytest.approx(1000.0 - reserved)
        assert balance["USDT"]["total"] == pytest.approx(1000.0)

    def test_open_orders_listing(self, simulator):
        order = simulator.simulate_order("SOL/USDT", "limit", "buy", 1.0, 90.0)
        open_orders = simulator.get_open_orders()
        assert [o["id"] for o in open_orders] == [order["id"]]
        assert simulator.get_open_orders("SOL/USDT") == open_orders
        assert simulator.get_open_orders("BTC/USDT") == []

    def test_cancel_releases_reserved_funds(self, simulator):
        order = simulator.simulate_order("SOL/USDT", "limit", "buy", 1.0, 90.0)
        cancelled = simulator.cancel_order(order["id"])
        assert cancelled["status"] == "canceled"
        assert simulator.get_open_orders() == []
        balance = simulator.get_balance()
        assert balance["USDT"]["free"] == pytest.approx(1000.0)
        assert balance["USDT"]["used"] == pytest.approx(0.0)

    def test_cancel_unknown_order(self, simulator):
        with pytest.raises(ccxt.OrderNotFound):
            simulator.cancel_order("nope")

    def test_limit_order_requires_price(self, simulator):
        with pytest.raises(ccxt.InvalidOrder):
            simulator.simulate_order("SOL/USDT", "limit", "buy", 1.0)


class TestValidationAndFormat:
    def test_invalid_side_and_type(self, simulator):
        with pytest.raises(ccxt.InvalidOrder):
            simulator.simulate_order("SOL/USDT", "market", "hold", 1.0)
        with pytest.raises(ccxt.InvalidOrder):
            simulator.simulate_order("SOL/USDT", "stop", "buy", 1.0)
        with pytest.raises(ccxt.InvalidOrder):
            simulator.simulate_order("SOL/USDT", "market", "buy", 0)

    def test_invalid_symbol(self, simulator):
        with pytest.raises(ccxt.BadSymbol):
            simulator.simulate_order("SOLUSDT", "market", "buy", 1.0)

    def test_get_balance_ccxt_format(self, simulator):
        balance = simulator.get_balance()
        assert set(balance) >= {"info", "free", "used", "total", "USDT"}
        assert balance["USDT"] == {"free": 1000.0, "used": 0.0, "total": 1000.0}
        assert balance["free"]["USDT"] == 1000.0
        assert balance["total"]["USDT"] == 1000.0

    def test_order_is_ccxt_format(self, simulator):
        order = simulator.simulate_order("SOL/USDT", "market", "buy", 1.0)
        for key in (
            "id",
            "timestamp",
            "datetime",
            "symbol",
            "type",
            "side",
            "price",
            "average",
            "amount",
            "filled",
            "remaining",
            "cost",
            "status",
            "fee",
            "info",
        ):
            assert key in order, f"missing CCXT order key: {key}"
        assert order["symbol"] == "SOL/USDT"
        assert order["info"]["paper_trading"] is True

    def test_orders_get_unique_ids(self, simulator):
        first = simulator.simulate_order("SOL/USDT", "market", "buy", 1.0)
        second = simulator.simulate_order("SOL/USDT", "market", "buy", 1.0)
        assert first["id"] != second["id"]
