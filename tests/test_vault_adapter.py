"""
Tests for the nerdbot_vault adapter and the vault HTTP client.

These tests are network-free: the vault client is mocked (or backed by an
httpx.MockTransport) and market data clients are MagicMocks. They are also
runnable without freqtrade installed - the adapter core has no freqtrade
dependency.
"""

import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import ccxt
import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from user_data.exchange.nerdbot_vault import (  # noqa: E402
    FREQTRADE_AVAILABLE,
    NerdbotVaultAdapter,
    register,
    register_ccxt_shim,
)
from user_data.exchange.vault_http_client import VaultHTTPClient  # noqa: E402


VAULT_KEY_ID = "11111111-1111-1111-1111-111111111111"
BOT_ID = "22222222-2222-2222-2222-222222222222"
TOKEN = "super-secret-backend-token"
INSTANCE_ID = "backend-instance-1"


@pytest.fixture
def vault_env(monkeypatch):
    monkeypatch.setenv("VAULT_BASE_URL", "https://vault.test")
    monkeypatch.setenv("VAULT_KEY_ID", VAULT_KEY_ID)
    monkeypatch.setenv("BACKEND_TOKEN", TOKEN)
    monkeypatch.setenv("BACKEND_INSTANCE_ID", INSTANCE_ID)
    monkeypatch.setenv("REAL_EXCHANGE", "binance")
    monkeypatch.setenv("BOT_ID", BOT_ID)
    monkeypatch.delenv("IS_PAPER_TRADING", raising=False)


def make_vault_client_mock():
    """Vault client mock issuing a distinct lease id per acquisition."""
    client = MagicMock(spec=VaultHTTPClient)
    counter = {"n": 0}

    def _acquire(vault_key_id, bot_id, lease_ttl_seconds=30):
        counter["n"] += 1
        return f"lease-{counter['n']}"

    client.acquire_lease.side_effect = _acquire
    client.validate_credentials.return_value = {"status": "valid"}
    client.place_order.return_value = {
        "order_id": "EX-1",
        "status": "open",
        "filled_amount": "0",
        "avg_price": None,
    }
    client.cancel_order.return_value = {"status": "cancelled"}
    client.get_balances.return_value = {"balances": []}
    client.query_order.return_value = {
        "order_id": "EX-1",
        "status": "open",
        "filled_amount": "0",
    }
    client.get_open_orders.return_value = []
    return client


def make_adapter(vault_client=None, market_client=None, **kwargs):
    return NerdbotVaultAdapter(
        vault_client=vault_client or make_vault_client_mock(),
        market_client=market_client or MagicMock(),
        **kwargs,
    )


# =============================================================================
# Startup / credential validation
# =============================================================================


class TestStartupValidation:
    def test_init_acquires_lease_and_validates(self, vault_env):
        client = make_vault_client_mock()
        make_adapter(vault_client=client)
        client.acquire_lease.assert_called_once_with(VAULT_KEY_ID, BOT_ID)
        client.validate_credentials.assert_called_once_with(
            VAULT_KEY_ID, "lease-1", BOT_ID, "binance"
        )

    def test_init_fails_hard_on_invalid_credentials(self, vault_env):
        client = make_vault_client_mock()
        client.validate_credentials.return_value = {"status": "invalid"}
        with pytest.raises(ccxt.AuthenticationError):
            make_adapter(vault_client=client)

    def test_init_fails_hard_on_lease_failure(self, vault_env):
        client = make_vault_client_mock()
        client.acquire_lease.side_effect = ccxt.AuthenticationError("no lease")
        with pytest.raises(ccxt.AuthenticationError):
            make_adapter(vault_client=client)

    def test_missing_env_fails(self, vault_env, monkeypatch):
        monkeypatch.delenv("VAULT_KEY_ID")
        with pytest.raises(ValueError, match="VAULT_KEY_ID"):
            make_adapter()

    def test_unsupported_real_exchange_fails(self, vault_env, monkeypatch):
        monkeypatch.setenv("REAL_EXCHANGE", "mtgox")
        with pytest.raises(ValueError, match="REAL_EXCHANGE"):
            make_adapter()


# =============================================================================
# Order routing through the vault
# =============================================================================


class TestOrderRouting:
    def test_create_order_routes_through_vault(self, vault_env):
        client = make_vault_client_mock()
        adapter = make_adapter(vault_client=client)
        order = adapter.create_order("SOL/USDT", "limit", "buy", 2.0, 100.0)

        kwargs = client.place_order.call_args.kwargs
        generated_id = kwargs.pop("client_order_id")
        assert kwargs == {
            "vault_key_id": VAULT_KEY_ID,
            "lease_id": "lease-2",  # fresh lease, not the init lease
            "bot_id": BOT_ID,
            "exchange": "binance",
            "symbol": "SOL/USDT",
            "side": "buy",
            "type": "limit",
            "amount": 2.0,
            "price": 100.0,
        }
        # A client order id is ALWAYS sent (exchange-side dedupe makes the
        # vault->exchange leg retry-safe); minted ids satisfy the strictest
        # exchange format: alphanumeric, 32 chars.
        assert re.fullmatch(r"nb[0-9a-f]{30}", generated_id)
        assert order["id"] == "EX-1"
        assert order["status"] == "open"
        assert order["symbol"] == "SOL/USDT"
        assert order["amount"] == 2.0
        assert order["remaining"] == 2.0

    def test_caller_supplied_client_order_id_passes_through(self, vault_env):
        client = make_vault_client_mock()
        adapter = make_adapter(vault_client=client)
        adapter.create_order(
            "SOL/USDT", "limit", "buy", 2.0, 100.0, {"clientOrderId": "mycustomid1"}
        )
        assert client.place_order.call_args.kwargs["client_order_id"] == "mycustomid1"

    def test_minted_client_order_ids_are_unique_per_call(self, vault_env):
        client = make_vault_client_mock()
        adapter = make_adapter(vault_client=client)
        adapter.create_order("SOL/USDT", "limit", "buy", 2.0, 100.0)
        adapter.create_order("SOL/USDT", "limit", "buy", 2.0, 100.0)
        first, second = (c.kwargs["client_order_id"] for c in client.place_order.call_args_list)
        assert first != second

    def test_filled_vault_status_maps_to_closed(self, vault_env):
        client = make_vault_client_mock()
        client.place_order.return_value = {
            "order_id": "EX-2",
            "status": "filled",
            "filled_amount": "2.0",
            "avg_price": "101.5",
        }
        adapter = make_adapter(vault_client=client)
        order = adapter.create_order("SOL/USDT", "market", "buy", 2.0)
        assert order["status"] == "closed"
        assert order["filled"] == 2.0
        assert order["average"] == 101.5
        assert order["cost"] == pytest.approx(203.0)
        assert order["remaining"] == 0.0

    def test_rejected_order_raises_invalid_order(self, vault_env):
        client = make_vault_client_mock()
        client.place_order.return_value = {
            "order_id": "EX-3",
            "status": "rejected",
            "filled_amount": "0",
            "avg_price": None,
        }
        adapter = make_adapter(vault_client=client)
        with pytest.raises(ccxt.InvalidOrder):
            adapter.create_order("SOL/USDT", "limit", "buy", 1.0, 50.0)

    @pytest.mark.parametrize("malformed", [{}, {"order_id": ""}, {"order_id": None}])
    def test_missing_order_id_raises_exchange_error(self, vault_env, malformed):
        client = make_vault_client_mock()
        client.place_order.return_value = {
            "status": "open",
            "filled_amount": "0",
            "avg_price": None,
            **malformed,
        }
        adapter = make_adapter(vault_client=client)
        with pytest.raises(ccxt.ExchangeError, match="missing order_id"):
            adapter.create_order("SOL/USDT", "limit", "buy", 1.0, 50.0)

    def test_cancel_order_routes_through_vault(self, vault_env):
        client = make_vault_client_mock()
        adapter = make_adapter(vault_client=client)
        result = adapter.cancel_order("EX-1", "SOL/USDT")
        client.cancel_order.assert_called_once_with(
            vault_key_id=VAULT_KEY_ID,
            lease_id="lease-2",
            bot_id=BOT_ID,
            exchange="binance",
            order_id="EX-1",
            # Binance requires the symbol on cancel; must always be forwarded.
            symbol="SOL/USDT",
        )
        assert result["status"] == "canceled"
        assert result["id"] == "EX-1"

    def test_cancel_not_found_raises_order_not_found(self, vault_env):
        client = make_vault_client_mock()
        client.cancel_order.return_value = {"status": "not_found"}
        adapter = make_adapter(vault_client=client)
        with pytest.raises(ccxt.OrderNotFound):
            adapter.cancel_order("missing", "SOL/USDT")

    def test_cancel_rejected_raises_invalid_order(self, vault_env):
        client = make_vault_client_mock()
        client.cancel_order.return_value = {"status": "rejected"}
        adapter = make_adapter(vault_client=client)
        with pytest.raises(ccxt.InvalidOrder):
            adapter.cancel_order("EX-1", "SOL/USDT")


# =============================================================================
# Lease lifecycle: fresh lease per call, never cached
# =============================================================================


class TestLeaseLifecycle:
    def test_fresh_lease_for_every_vault_call(self, vault_env):
        client = make_vault_client_mock()
        adapter = make_adapter(vault_client=client)  # lease-1 (validation)

        adapter.create_order("SOL/USDT", "limit", "buy", 1.0, 100.0)  # lease-2
        adapter.fetch_balance()  # lease-3
        adapter.cancel_order("EX-1", "SOL/USDT")  # lease-4

        assert client.acquire_lease.call_count == 4
        assert client.place_order.call_args.kwargs["lease_id"] == "lease-2"
        assert client.get_balances.call_args.kwargs["lease_id"] == "lease-3"
        assert client.cancel_order.call_args.kwargs["lease_id"] == "lease-4"

    def test_lease_is_never_reused_across_repeated_calls(self, vault_env):
        client = make_vault_client_mock()
        adapter = make_adapter(vault_client=client)
        adapter.fetch_balance()
        adapter.fetch_balance()
        leases = [call.kwargs["lease_id"] for call in client.get_balances.call_args_list]
        assert leases == ["lease-2", "lease-3"]
        assert len(set(leases)) == len(leases)

    def test_lease_failure_raises_authentication_error(self, vault_env):
        client = make_vault_client_mock()
        adapter = make_adapter(vault_client=client)
        client.acquire_lease.side_effect = ccxt.AuthenticationError("lease denied")
        with pytest.raises(ccxt.AuthenticationError):
            adapter.create_order("SOL/USDT", "limit", "buy", 1.0, 100.0)
        with pytest.raises(ccxt.AuthenticationError):
            adapter.fetch_balance()
        client.place_order.assert_not_called()
        client.get_balances.assert_not_called()


# =============================================================================
# Balances: CCXT format conversion
# =============================================================================


class TestBalances:
    def test_fetch_balance_converts_to_ccxt_format(self, vault_env):
        client = make_vault_client_mock()
        client.get_balances.return_value = {
            "balances": [
                {"asset": "BTC", "available": "0.5", "locked": "0.25"},
                {"asset": "USDT", "available": "1000", "locked": "0"},
            ]
        }
        adapter = make_adapter(vault_client=client)
        balance = adapter.fetch_balance()

        assert balance["BTC"] == {"free": 0.5, "used": 0.25, "total": 0.75}
        assert balance["USDT"] == {"free": 1000.0, "used": 0.0, "total": 1000.0}
        assert balance["free"] == {"BTC": 0.5, "USDT": 1000.0}
        assert balance["used"] == {"BTC": 0.25, "USDT": 0.0}
        assert balance["total"] == {"BTC": 0.75, "USDT": 1000.0}
        assert "info" in balance

    def test_fetch_balance_empty(self, vault_env):
        adapter = make_adapter()
        balance = adapter.fetch_balance()
        assert balance["free"] == {} and balance["used"] == {} and balance["total"] == {}

    def test_balance_entry_missing_asset_raises_exchange_error(self, vault_env):
        client = make_vault_client_mock()
        client.get_balances.return_value = {"balances": [{"available": "1", "locked": "0"}]}
        adapter = make_adapter(vault_client=client)
        with pytest.raises(ccxt.ExchangeError, match="missing asset"):
            adapter.fetch_balance()


# =============================================================================
# Order state (fetch_order / fetch_open_orders): vault delegation + conversion
# =============================================================================


def make_order_entry(**overrides) -> dict:
    """A representative vault OrderEntry (wire format: decimals as strings)."""
    entry = {
        "order_id": "EX-7",
        "status": "open",
        "symbol": "SOLUSD",  # exchange-normalised (Kraken style)
        "side": "buy",
        "type": "limit",
        "amount": "2.0",
        "filled_amount": "0.5",
        "remaining": "1.5",
        "price": "100.0",
        "avg_price": "99.5",
    }
    entry.update(overrides)
    return entry


class TestOrderStateRouting:
    def test_fetch_order_routes_through_vault_with_fresh_lease(self, vault_env):
        client = make_vault_client_mock()
        client.query_order.return_value = make_order_entry()
        adapter = make_adapter(vault_client=client)

        order = adapter.fetch_order("EX-7", "SOL/USD")

        client.query_order.assert_called_once_with(
            vault_key_id=VAULT_KEY_ID,
            bot_id=BOT_ID,
            lease_id="lease-2",  # fresh lease, not the init lease
            order_id="EX-7",
            exchange="binance",
            symbol="SOL/USD",
        )
        assert order["id"] == "EX-7"
        assert order["status"] == "open"
        assert order["side"] == "buy"
        assert order["type"] == "limit"

    def test_fetch_order_converts_entry_to_ccxt_structure(self, vault_env):
        client = make_vault_client_mock()
        entry = make_order_entry()
        client.query_order.return_value = entry
        adapter = make_adapter(vault_client=client)

        order = adapter.fetch_order("EX-7", "SOL/USD")

        # Requested ccxt symbol wins over the exchange-normalised "SOLUSD".
        assert order["symbol"] == "SOL/USD"
        # Numeric coercion: wire decimals (strings) -> floats.
        assert order["price"] == 100.0
        assert order["average"] == 99.5
        assert order["amount"] == 2.0
        assert order["filled"] == 0.5
        assert order["remaining"] == 1.5
        assert order["clientOrderId"] is None
        assert order["timestamp"] is None
        assert order["datetime"] is None
        assert order["fee"] is None
        assert order["trades"] == []
        assert order["info"] is entry

    def test_fetch_order_falls_back_to_vault_symbol_when_not_requested(self, vault_env):
        client = make_vault_client_mock()
        client.query_order.return_value = make_order_entry()
        adapter = make_adapter(vault_client=client)
        order = adapter.fetch_order("EX-7")
        assert order["symbol"] == "SOLUSD"
        assert client.query_order.call_args.kwargs["symbol"] is None

    @pytest.mark.parametrize(
        ("vault_status", "ccxt_status"),
        [("open", "open"), ("closed", "closed"), ("cancelled", "canceled")],
    )
    def test_fetch_order_status_mapping(self, vault_env, vault_status, ccxt_status):
        client = make_vault_client_mock()
        client.query_order.return_value = make_order_entry(status=vault_status)
        adapter = make_adapter(vault_client=client)
        assert adapter.fetch_order("EX-7", "SOL/USD")["status"] == ccxt_status

    def test_fetch_order_handles_minimal_entry(self, vault_env):
        client = make_vault_client_mock()
        client.query_order.return_value = {
            "order_id": "EX-8",
            "status": "open",
            "filled_amount": "0",
        }
        adapter = make_adapter(vault_client=client)
        order = adapter.fetch_order("EX-8", "SOL/USD")
        assert order["filled"] == 0.0
        assert order["price"] is None
        assert order["average"] is None
        assert order["amount"] is None
        assert order["remaining"] is None
        assert order["side"] is None
        assert order["type"] is None

    def test_fetch_order_missing_order_id_raises_exchange_error(self, vault_env):
        client = make_vault_client_mock()
        client.query_order.return_value = {"status": "open", "filled_amount": "0"}
        adapter = make_adapter(vault_client=client)
        with pytest.raises(ccxt.ExchangeError, match="missing order_id"):
            adapter.fetch_order("EX-9", "SOL/USD")

    def test_fetch_open_orders_routes_through_vault_with_fresh_lease(self, vault_env):
        client = make_vault_client_mock()
        client.get_open_orders.return_value = [
            make_order_entry(),
            make_order_entry(order_id="EX-8", status="cancelled"),
        ]
        adapter = make_adapter(vault_client=client)

        orders = adapter.fetch_open_orders("SOL/USD")

        client.get_open_orders.assert_called_once_with(
            vault_key_id=VAULT_KEY_ID,
            bot_id=BOT_ID,
            lease_id="lease-2",
            exchange="binance",
            symbol="SOL/USD",
        )
        assert [o["id"] for o in orders] == ["EX-7", "EX-8"]
        # Requested symbol preference applies to every converted order.
        assert all(o["symbol"] == "SOL/USD" for o in orders)
        assert orders[1]["status"] == "canceled"

    def test_fetch_open_orders_without_symbol(self, vault_env):
        client = make_vault_client_mock()
        client.get_open_orders.return_value = [make_order_entry()]
        adapter = make_adapter(vault_client=client)
        orders = adapter.fetch_open_orders()
        assert client.get_open_orders.call_args.kwargs["symbol"] is None
        assert orders[0]["symbol"] == "SOLUSD"

    def test_fresh_lease_per_order_state_call(self, vault_env):
        client = make_vault_client_mock()
        client.query_order.return_value = make_order_entry()
        adapter = make_adapter(vault_client=client)  # lease-1 (validation)

        adapter.fetch_order("EX-7", "SOL/USD")  # lease-2
        adapter.fetch_open_orders("SOL/USD")  # lease-3
        adapter.fetch_order("EX-7", "SOL/USD")  # lease-4

        assert client.acquire_lease.call_count == 4
        leases = [call.kwargs["lease_id"] for call in client.query_order.call_args_list]
        assert leases == ["lease-2", "lease-4"]
        assert client.get_open_orders.call_args.kwargs["lease_id"] == "lease-3"

    def test_lease_failure_blocks_order_queries(self, vault_env):
        client = make_vault_client_mock()
        adapter = make_adapter(vault_client=client)
        client.acquire_lease.side_effect = ccxt.AuthenticationError("lease denied")
        with pytest.raises(ccxt.AuthenticationError):
            adapter.fetch_order("EX-7", "SOL/USD")
        with pytest.raises(ccxt.AuthenticationError):
            adapter.fetch_open_orders("SOL/USD")
        client.query_order.assert_not_called()
        client.get_open_orders.assert_not_called()

    def test_vault_auth_failure_propagates(self, vault_env):
        client = make_vault_client_mock()
        client.query_order.side_effect = ccxt.AuthenticationError("rejected")
        client.get_open_orders.side_effect = ccxt.AuthenticationError("rejected")
        adapter = make_adapter(vault_client=client)
        with pytest.raises(ccxt.AuthenticationError):
            adapter.fetch_order("EX-7", "SOL/USD")
        with pytest.raises(ccxt.AuthenticationError):
            adapter.fetch_open_orders("SOL/USD")


# =============================================================================
# Paper trading mode: vault NEVER called for orders
# =============================================================================


class TestPaperTradingMode:
    @pytest.fixture
    def paper_adapter(self, vault_env, monkeypatch):
        monkeypatch.setenv("IS_PAPER_TRADING", "true")
        market = MagicMock()
        market.fetch_ticker.return_value = {"last": 100.0}
        client = make_vault_client_mock()
        adapter = make_adapter(vault_client=client, market_client=market)
        return adapter, client

    def test_paper_orders_never_call_vault(self, paper_adapter):
        adapter, client = paper_adapter
        baseline_leases = client.acquire_lease.call_count  # init validation only
        order = adapter.create_order("SOL/USDT", "market", "buy", 2.0)
        assert order["status"] == "closed"
        assert order["info"].get("paper_trading") is True
        client.place_order.assert_not_called()
        assert client.acquire_lease.call_count == baseline_leases

    def test_paper_cancel_never_calls_vault(self, paper_adapter):
        adapter, client = paper_adapter
        order = adapter.create_order("SOL/USDT", "limit", "buy", 1.0, 50.0)  # resting
        adapter.cancel_order(order["id"], "SOL/USDT")
        client.cancel_order.assert_not_called()

    def test_paper_balance_comes_from_simulator(self, paper_adapter):
        adapter, client = paper_adapter
        balance = adapter.fetch_balance()
        client.get_balances.assert_not_called()
        assert balance["info"].get("paper_trading") is True
        assert balance["USDT"]["free"] == 1000.0

    def test_paper_open_orders_come_from_simulator(self, paper_adapter):
        adapter, client = paper_adapter
        order = adapter.create_order("SOL/USDT", "limit", "buy", 1.0, 50.0)
        open_orders = adapter.fetch_open_orders("SOL/USDT")
        assert [o["id"] for o in open_orders] == [order["id"]]
        client.get_open_orders.assert_not_called()

    def test_paper_order_state_never_calls_vault(self, paper_adapter):
        adapter, client = paper_adapter
        order = adapter.create_order("SOL/USDT", "limit", "buy", 1.0, 50.0)  # resting
        baseline_leases = client.acquire_lease.call_count  # init validation only

        fetched = adapter.fetch_order(order["id"])
        open_orders = adapter.fetch_open_orders("SOL/USDT")

        assert fetched["id"] == order["id"]
        assert fetched["status"] == "open"
        assert [o["id"] for o in open_orders] == [order["id"]]
        client.query_order.assert_not_called()
        client.get_open_orders.assert_not_called()
        assert client.acquire_lease.call_count == baseline_leases

    def test_paper_fetch_order_unknown_id_raises_order_not_found(self, paper_adapter):
        adapter, client = paper_adapter
        with pytest.raises(ccxt.OrderNotFound):
            adapter.fetch_order("does-not-exist")
        client.query_order.assert_not_called()


# =============================================================================
# Market data delegation
# =============================================================================


class TestMarketDataDelegation:
    def test_fetch_ticker_delegates_to_market_client(self, vault_env):
        market = MagicMock()
        market.fetch_ticker.return_value = {"last": 42.0}
        adapter = make_adapter(market_client=market)
        assert adapter.fetch_ticker("SOL/USDT") == {"last": 42.0}
        market.fetch_ticker.assert_called_once_with("SOL/USDT")

    def test_fetch_ohlcv_delegates_to_market_client(self, vault_env):
        market = MagicMock()
        market.fetch_ohlcv.return_value = [[1, 2, 3, 4, 5, 6]]
        adapter = make_adapter(market_client=market)
        assert adapter.fetch_ohlcv("SOL/USDT", "5m", since=123, limit=10) == [[1, 2, 3, 4, 5, 6]]
        market.fetch_ohlcv.assert_called_once_with("SOL/USDT", "5m", since=123, limit=10)


# =============================================================================
# VaultHTTPClient: exact API contract (paths, headers, bodies)
# =============================================================================


def make_http_client(handler) -> VaultHTTPClient:
    client = VaultHTTPClient("https://vault.test", TOKEN, INSTANCE_ID)
    client._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=30.0)
    return client


class TestVaultHTTPClientContract:
    def test_acquire_lease_contract(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["headers"] = request.headers
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "lease_id": "33333333-3333-3333-3333-333333333333",
                    "vault_key_id": VAULT_KEY_ID,
                    "bot_id": BOT_ID,
                    "backend_instance_id": INSTANCE_ID,
                    "expires_at": "2026-06-11T00:00:30Z",
                },
            )

        client = make_http_client(handler)
        lease_id = client.acquire_lease(VAULT_KEY_ID, BOT_ID)

        assert lease_id == "33333333-3333-3333-3333-333333333333"
        assert seen["url"] == f"https://vault.test/v1/proxy/{VAULT_KEY_ID}/lease"
        assert seen["headers"]["Authorization"] == f"Bearer {TOKEN}"
        assert seen["headers"]["X-Backend-Instance-ID"] == INSTANCE_ID
        assert "X-Vault-Lease" not in seen["headers"]
        assert seen["body"] == {
            "bot_id": BOT_ID,
            "backend_instance_id": INSTANCE_ID,
            "lease_ttl_seconds": 30,
        }

    def test_place_order_contract(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["headers"] = request.headers
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "order_id": "EX-9",
                    "status": "open",
                    "filled_amount": "0",
                    "avg_price": None,
                },
            )

        client = make_http_client(handler)
        result = client.place_order(
            VAULT_KEY_ID,
            "lease-xyz",
            BOT_ID,
            "binance",
            symbol="SOL/USDT",
            side="buy",
            type="limit",
            amount=1.5,
            price=99.5,
        )

        assert result["order_id"] == "EX-9"
        assert seen["url"] == f"https://vault.test/v1/proxy/{VAULT_KEY_ID}/orders"
        assert seen["headers"]["X-Vault-Lease"] == "lease-xyz"
        assert seen["headers"]["Authorization"] == f"Bearer {TOKEN}"
        assert seen["headers"]["X-Backend-Instance-ID"] == INSTANCE_ID
        assert seen["body"] == {
            "bot_id": BOT_ID,
            "exchange": "binance",
            "symbol": "SOL/USDT",
            "side": "buy",
            "type": "limit",
            "amount": "1.5",
            "price": "99.5",
        }

    def test_cancel_order_contract(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"status": "cancelled"})

        client = make_http_client(handler)
        result = client.cancel_order(VAULT_KEY_ID, "lease-1", BOT_ID, "binance", "EX-9")
        assert result == {"status": "cancelled"}
        assert seen["url"] == f"https://vault.test/v1/proxy/{VAULT_KEY_ID}/orders/cancel"
        assert seen["body"] == {"bot_id": BOT_ID, "exchange": "binance", "order_id": "EX-9"}

    def test_cancel_order_contract_with_symbol(self):
        # Binance's cancel endpoint requires the symbol; the vault accepts an
        # optional "symbol" field and forwards it to the handler.
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"status": "cancelled"})

        client = make_http_client(handler)
        client.cancel_order(VAULT_KEY_ID, "lease-1", BOT_ID, "binance", "EX-9", symbol="SOL/USDT")
        assert seen["body"] == {
            "bot_id": BOT_ID,
            "exchange": "binance",
            "order_id": "EX-9",
            "symbol": "SOL/USDT",
        }

    def test_get_balances_contract(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200, json={"balances": [{"asset": "BTC", "available": "1", "locked": "0"}]}
            )

        client = make_http_client(handler)
        result = client.get_balances(VAULT_KEY_ID, "lease-1", BOT_ID, "binance")
        assert result["balances"][0]["asset"] == "BTC"
        assert seen["url"] == f"https://vault.test/v1/proxy/{VAULT_KEY_ID}/balances"
        assert seen["body"] == {"bot_id": BOT_ID, "exchange": "binance"}

    def test_query_order_contract(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["headers"] = request.headers
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "order": {
                        "order_id": "EX-7",
                        "status": "closed",
                        "symbol": "SOLUSD",
                        "side": "buy",
                        "type": "limit",
                        "amount": "2.0",
                        "filled_amount": "2.0",
                        "remaining": "0",
                        "price": "100.0",
                        "avg_price": "99.5",
                    }
                },
            )

        client = make_http_client(handler)
        order = client.query_order(
            VAULT_KEY_ID, BOT_ID, "lease-xyz", "EX-7", "kraken", symbol="SOLUSD"
        )

        # Returns the unwrapped OrderEntry.
        assert order["order_id"] == "EX-7"
        assert order["status"] == "closed"
        assert seen["url"] == f"https://vault.test/v1/proxy/{VAULT_KEY_ID}/orders/query"
        assert seen["headers"]["Authorization"] == f"Bearer {TOKEN}"
        assert seen["headers"]["X-Backend-Instance-ID"] == INSTANCE_ID
        assert seen["headers"]["X-Vault-Lease"] == "lease-xyz"
        assert seen["body"] == {
            "bot_id": BOT_ID,
            "exchange": "kraken",
            "order_id": "EX-7",
            "symbol": "SOLUSD",
        }

    def test_query_order_omits_symbol_when_not_provided(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200, json={"order": {"order_id": "EX-7", "status": "open", "filled_amount": "0"}}
            )

        client = make_http_client(handler)
        client.query_order(VAULT_KEY_ID, BOT_ID, "lease-1", "EX-7", "binance")
        assert seen["body"] == {"bot_id": BOT_ID, "exchange": "binance", "order_id": "EX-7"}

    def test_query_order_missing_order_raises_exchange_error(self):
        client = make_http_client(lambda request: httpx.Response(200, json={}))
        with pytest.raises(ccxt.ExchangeError, match="missing order"):
            client.query_order(VAULT_KEY_ID, BOT_ID, "lease-1", "EX-7", "binance")

    def test_get_open_orders_contract(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["headers"] = request.headers
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "orders": [
                        {"order_id": "EX-7", "status": "open", "filled_amount": "0"},
                        {"order_id": "EX-8", "status": "open", "filled_amount": "1.5"},
                    ]
                },
            )

        client = make_http_client(handler)
        orders = client.get_open_orders(
            VAULT_KEY_ID, BOT_ID, "lease-abc", "kraken", symbol="SOLUSD"
        )

        # Returns the unwrapped list of OrderEntry dicts.
        assert [o["order_id"] for o in orders] == ["EX-7", "EX-8"]
        assert seen["url"] == f"https://vault.test/v1/proxy/{VAULT_KEY_ID}/orders/open"
        assert seen["headers"]["Authorization"] == f"Bearer {TOKEN}"
        assert seen["headers"]["X-Backend-Instance-ID"] == INSTANCE_ID
        assert seen["headers"]["X-Vault-Lease"] == "lease-abc"
        assert seen["body"] == {"bot_id": BOT_ID, "exchange": "kraken", "symbol": "SOLUSD"}

    def test_get_open_orders_omits_symbol_when_not_provided(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"orders": []})

        client = make_http_client(handler)
        assert client.get_open_orders(VAULT_KEY_ID, BOT_ID, "lease-1", "binance") == []
        assert seen["body"] == {"bot_id": BOT_ID, "exchange": "binance"}

    def test_get_open_orders_missing_orders_raises_exchange_error(self):
        client = make_http_client(lambda request: httpx.Response(200, json={}))
        with pytest.raises(ccxt.ExchangeError, match="missing orders"):
            client.get_open_orders(VAULT_KEY_ID, BOT_ID, "lease-1", "binance")

    @pytest.mark.parametrize("status_code", [401, 403])
    def test_auth_rejection_raises_authentication_error(self, status_code):
        client = make_http_client(lambda request: httpx.Response(status_code, json={}))
        with pytest.raises(ccxt.AuthenticationError):
            client.get_balances(VAULT_KEY_ID, "lease-1", BOT_ID, "binance")

    @pytest.mark.parametrize("status_code", [401, 403])
    def test_query_order_auth_rejection_raises_authentication_error(self, status_code):
        client = make_http_client(lambda request: httpx.Response(status_code, json={}))
        with pytest.raises(ccxt.AuthenticationError):
            client.query_order(VAULT_KEY_ID, BOT_ID, "lease-1", "EX-7", "binance")

    @pytest.mark.parametrize("status_code", [401, 403])
    def test_get_open_orders_auth_rejection_raises_authentication_error(self, status_code):
        client = make_http_client(lambda request: httpx.Response(status_code, json={}))
        with pytest.raises(ccxt.AuthenticationError):
            client.get_open_orders(VAULT_KEY_ID, BOT_ID, "lease-1", "binance")

    def test_rate_limit_raises_rate_limit_exceeded(self):
        client = make_http_client(lambda request: httpx.Response(429, json={}))
        with pytest.raises(ccxt.RateLimitExceeded):
            client.place_order(
                VAULT_KEY_ID,
                "l",
                BOT_ID,
                "binance",
                symbol="SOL/USDT",
                side="buy",
                type="market",
                amount=1,
            )

    def test_any_lease_failure_raises_authentication_error(self):
        client = make_http_client(lambda request: httpx.Response(500, json={}))
        with pytest.raises(ccxt.AuthenticationError):
            client.acquire_lease(VAULT_KEY_ID, BOT_ID)

    def test_network_failure_on_lease_raises_authentication_error(self):
        def handler(request):
            raise httpx.ConnectError("boom")

        client = make_http_client(handler)
        with pytest.raises(ccxt.AuthenticationError):
            client.acquire_lease(VAULT_KEY_ID, BOT_ID)

    def test_token_never_in_repr_or_errors(self):
        client = make_http_client(lambda request: httpx.Response(401, json={}))
        assert TOKEN not in repr(client)
        with pytest.raises(ccxt.AuthenticationError) as excinfo:
            client.get_balances(VAULT_KEY_ID, "lease-1", BOT_ID, "binance")
        assert TOKEN not in str(excinfo.value)


# =============================================================================
# Resource cleanup: close() chain releases the httpx socket pool
# =============================================================================


class TestCloseChain:
    @pytest.fixture(autouse=True)
    def _flush_pending_finalizers(self):
        # Exchange.__del__ calls self.close(); bare Nerdbot_Vault instances
        # from earlier tests can finalize INSIDE the patch.object windows
        # below (observed on Python 3.12 and 3.14 GC timing), recording
        # phantom extra close calls. Flush pending finalizers first so only
        # each test's own calls count.
        import gc

        gc.collect()

    def test_adapter_close_closes_vault_client(self, vault_env):
        client = make_vault_client_mock()
        adapter = make_adapter(vault_client=client)
        adapter.close()
        client.close.assert_called_once_with()

    def test_adapter_close_is_idempotent(self, vault_env):
        client = make_vault_client_mock()
        adapter = make_adapter(vault_client=client)
        adapter.close()
        adapter.close()  # must not raise
        assert client.close.call_count == 2

    def test_http_client_close_closes_underlying_client_and_is_idempotent(self):
        client = make_http_client(lambda request: httpx.Response(200, json={}))
        underlying = client._client
        assert not underlying.is_closed
        client.close()
        assert underlying.is_closed
        client.close()  # second close must not raise

    @pytest.mark.skipif(not FREQTRADE_AVAILABLE, reason="freqtrade not installed")
    def test_freqtrade_close_chains_adapter_then_super(self, vault_env):
        from unittest.mock import patch

        from freqtrade.exchange import Exchange
        from user_data.exchange.nerdbot_vault import Nerdbot_Vault

        exchange = object.__new__(Nerdbot_Vault)
        # Attributes Exchange.close()/__del__ dereference at GC time.
        exchange._exchange_ws = None
        exchange._ws_async = None
        exchange.loop = None
        exchange._vault_adapter = MagicMock()
        with patch.object(Exchange, "close") as super_close:
            exchange.close()
        exchange._vault_adapter.close.assert_called_once_with()
        super_close.assert_called_once_with()

    @pytest.mark.skipif(not FREQTRADE_AVAILABLE, reason="freqtrade not installed")
    def test_freqtrade_close_still_calls_super_if_adapter_close_fails(self, vault_env):
        from unittest.mock import patch

        from freqtrade.exchange import Exchange
        from user_data.exchange.nerdbot_vault import Nerdbot_Vault

        exchange = object.__new__(Nerdbot_Vault)
        # Attributes Exchange.close()/__del__ dereference at GC time.
        exchange._exchange_ws = None
        exchange._ws_async = None
        exchange.loop = None
        exchange._vault_adapter = MagicMock()
        exchange._vault_adapter.close.side_effect = RuntimeError("boom")
        with patch.object(Exchange, "close") as super_close:
            exchange.close()  # must not raise
        super_close.assert_called_once_with()


# =============================================================================
# Freqtrade-level order-state routing (Nerdbot_Vault overrides)
# =============================================================================


@pytest.mark.skipif(not FREQTRADE_AVAILABLE, reason="freqtrade not installed")
class TestFreqtradeOrderStateRouting:
    @staticmethod
    def make_exchange(dry_run: bool):
        from user_data.exchange.nerdbot_vault import Nerdbot_Vault

        exchange = object.__new__(Nerdbot_Vault)
        exchange._config = {"dry_run": dry_run}
        exchange.log_responses = False
        exchange._vault_adapter = MagicMock()
        # Spot pairs: contract size 1 leaves orders untouched.
        exchange.get_contract_size = lambda pair: 1.0
        # Attributes Exchange.close()/__del__ dereference at GC time.
        exchange._exchange_ws = None
        exchange._ws_async = None
        exchange.loop = None
        return exchange

    def test_live_fetch_order_routes_via_adapter(self, vault_env):
        exchange = self.make_exchange(dry_run=False)
        exchange._vault_adapter.fetch_order.return_value = {
            "id": "EX-7",
            "symbol": "SOL/USD",
            "status": "open",
        }
        order = exchange.fetch_order("EX-7", "SOL/USD")
        exchange._vault_adapter.fetch_order.assert_called_once_with("EX-7", "SOL/USD")
        assert order["id"] == "EX-7"

    def test_dry_run_fetch_order_uses_local_simulation(self, vault_env):
        from unittest.mock import patch

        from freqtrade.exchange import Exchange

        exchange = self.make_exchange(dry_run=True)
        with patch.object(Exchange, "fetch_order", return_value={"id": "dry-1"}) as super_fetch:
            order = exchange.fetch_order("dry-1", "SOL/USD")
        super_fetch.assert_called_once_with("dry-1", "SOL/USD", None)
        exchange._vault_adapter.fetch_order.assert_not_called()
        assert order["id"] == "dry-1"

    def test_live_fetch_open_orders_routes_via_adapter(self, vault_env):
        exchange = self.make_exchange(dry_run=False)
        exchange._vault_adapter.fetch_open_orders.return_value = [
            {"id": "EX-7", "symbol": "SOL/USD", "status": "open"}
        ]
        orders = exchange.fetch_open_orders("SOL/USD")
        exchange._vault_adapter.fetch_open_orders.assert_called_once_with("SOL/USD")
        assert [o["id"] for o in orders] == ["EX-7"]

    def test_dry_run_fetch_open_orders_serves_local_orders(self, vault_env):
        exchange = self.make_exchange(dry_run=True)
        exchange._dry_run_open_orders = {
            "1": {"id": "1", "status": "open", "symbol": "SOL/USD"},
            "2": {"id": "2", "status": "closed", "symbol": "SOL/USD"},
            "3": {"id": "3", "status": "open", "symbol": "BTC/USD"},
        }
        orders = exchange.fetch_open_orders("SOL/USD")
        assert [o["id"] for o in orders] == ["1"]
        # Without a pair filter, all open orders are returned.
        all_orders = exchange.fetch_open_orders()
        assert sorted(o["id"] for o in all_orders) == ["1", "3"]
        exchange._vault_adapter.fetch_open_orders.assert_not_called()

    def test_live_fetch_order_not_found_maps_to_retryable(self, vault_env):
        from freqtrade.exceptions import RetryableOrderError

        exchange = self.make_exchange(dry_run=False)
        exchange._vault_adapter.fetch_order.side_effect = ccxt.OrderNotFound("missing")
        # count=0 disables the retrier's backoff loop for the test.
        with pytest.raises(RetryableOrderError):
            exchange.fetch_order("EX-7", "SOL/USD", count=0)

    def test_live_fetch_order_exchange_error_maps_to_temporary(self, vault_env):
        from freqtrade.exceptions import TemporaryError

        exchange = self.make_exchange(dry_run=False)
        exchange._vault_adapter.fetch_order.side_effect = ccxt.ExchangeError("boom")
        with pytest.raises(TemporaryError):
            exchange.fetch_order("EX-7", "SOL/USD", count=0)

    def test_live_fetch_open_orders_auth_error_maps_to_temporary(self, vault_env):
        # ccxt.AuthenticationError subclasses ccxt.ExchangeError, so it maps
        # to TemporaryError - identical to Freqtrade core's own mapping.
        from freqtrade.exceptions import TemporaryError

        exchange = self.make_exchange(dry_run=False)
        exchange._vault_adapter.fetch_open_orders.side_effect = ccxt.AuthenticationError("denied")
        with pytest.raises(TemporaryError):
            exchange.fetch_open_orders("SOL/USD")

    def test_live_fetch_open_orders_base_error_maps_to_operational(self, vault_env):
        from freqtrade.exceptions import OperationalException

        exchange = self.make_exchange(dry_run=False)
        exchange._vault_adapter.fetch_open_orders.side_effect = ccxt.BaseError("boom")
        with pytest.raises(OperationalException):
            exchange.fetch_open_orders("SOL/USD")


# =============================================================================
# REAL_EXCHANGE-parameterized _ft_has (data-correctness quirks)
# =============================================================================


@pytest.mark.skipif(not FREQTRADE_AVAILABLE, reason="freqtrade not installed")
class TestRealExchangeFtHas:
    """
    Nerdbot_Vault extends the GENERIC Exchange class, so per-exchange
    subclass quirks (freqtrade/exchange/kraken.py) must be re-applied for
    the data path - and ONLY the data path, since orders route through
    dry-run or the vault, never native exchange order endpoints.
    """

    #: The Kraken._ft_has entries the adapter mirrors (data path only).
    KRAKEN_DATA_KEYS = (
        "ohlcv_has_history",
        "trades_pagination",
        "trades_pagination_arg",
        "trades_pagination_overlap",
        "trades_has_history",
    )

    @staticmethod
    def build_ft_has(monkeypatch, real_exchange: str, exchange_conf: dict | None = None) -> dict:
        from freqtrade.enums import TradingMode
        from user_data.exchange.nerdbot_vault import Nerdbot_Vault

        monkeypatch.setenv("REAL_EXCHANGE", real_exchange)
        exchange = object.__new__(Nerdbot_Vault)
        exchange.trading_mode = TradingMode.SPOT
        # Attributes Exchange.close()/__del__ dereference at GC time.
        exchange._exchange_ws = None
        exchange._ws_async = None
        exchange.loop = None
        exchange.build_ft_has(exchange_conf or {})
        return exchange._ft_has

    def test_kraken_inherits_data_quirks(self, vault_env, monkeypatch):
        ft_has = self.build_ft_has(monkeypatch, "kraken")
        assert ft_has["ohlcv_has_history"] is False
        assert ft_has["trades_pagination"] == "id"
        assert ft_has["trades_pagination_arg"] == "since"
        assert ft_has["trades_pagination_overlap"] is False
        assert ft_has["trades_has_history"] is True

    def test_kraken_quirks_match_kraken_class_source(self, vault_env, monkeypatch):
        # Guard against upstream drift: the mirrored values must stay
        # identical to the real Kraken class for every mirrored key.
        from freqtrade.exchange.kraken import Kraken

        ft_has = self.build_ft_has(monkeypatch, "kraken")
        for key in self.KRAKEN_DATA_KEYS:
            assert ft_has[key] == Kraken._ft_has[key], key

    def test_kraken_does_not_inherit_order_execution_quirks(self, vault_env, monkeypatch):
        # Orders route through dry-run/vault - Kraken's native order quirks
        # (stoploss_on_exchange, IOC/PO time-in-force) must NOT come over.
        ft_has = self.build_ft_has(monkeypatch, "kraken")
        assert ft_has["stoploss_on_exchange"] is False
        assert ft_has["order_time_in_force"] == ["GTC"]
        assert ft_has["stoploss_order_types"] == {}

    def test_coinbase_keeps_generic_data_defaults(self, vault_env, monkeypatch):
        ft_has = self.build_ft_has(monkeypatch, "coinbase")
        assert ft_has["ohlcv_has_history"] is True
        assert ft_has["trades_pagination"] == "time"
        assert ft_has["stoploss_on_exchange"] is False

    def test_binance_keeps_generic_data_defaults(self, vault_env, monkeypatch):
        ft_has = self.build_ft_has(monkeypatch, "binance")
        assert ft_has["ohlcv_has_history"] is True

    def test_binance_inherits_l2_limit_range(self, vault_env, monkeypatch):
        # Binance's order-book endpoint only accepts discrete depth limits;
        # order_book_top: 1 must be rounded up (to 5), not forwarded raw.
        ft_has = self.build_ft_has(monkeypatch, "binance")
        assert ft_has["l2_limit_range"] == [5, 10, 20, 50, 100, 500, 1000]

    def test_binance_l2_limit_range_matches_binance_class_source(self, vault_env, monkeypatch):
        # Guard against upstream drift: identical to the real Binance class.
        from freqtrade.exchange.binance import Binance

        ft_has = self.build_ft_has(monkeypatch, "binance")
        assert ft_has["l2_limit_range"] == Binance._ft_has["l2_limit_range"]

    @pytest.mark.parametrize("real_exchange", ["kraken", "coinbase"])
    def test_l2_limit_range_stays_generic_elsewhere(self, vault_env, monkeypatch, real_exchange):
        # Upstream Kraken defines no l2_limit_range (checked against
        # freqtrade/exchange/kraken.py) and coinbase has no freqtrade
        # subclass - both keep the generic None (no limit rounding).
        from freqtrade.exchange.kraken import Kraken

        assert "l2_limit_range" not in Kraken._ft_has  # upstream-drift guard
        ft_has = self.build_ft_has(monkeypatch, real_exchange)
        assert ft_has["l2_limit_range"] is None

    @pytest.mark.parametrize("real_exchange", ["binance", "kraken", "coinbase"])
    def test_ws_disabled_for_every_real_exchange(self, vault_env, monkeypatch, real_exchange):
        ft_has = self.build_ft_has(monkeypatch, real_exchange)
        assert ft_has["ws_enabled"] is False

    def test_config_ft_has_params_still_win_over_quirks(self, vault_env, monkeypatch):
        # Explicit config-level overrides must keep the highest precedence.
        ft_has = self.build_ft_has(
            monkeypatch, "kraken", {"_ft_has_params": {"ohlcv_has_history": True}}
        )
        assert ft_has["ohlcv_has_history"] is True

    def test_unknown_real_exchange_leaves_defaults_untouched(self, vault_env, monkeypatch):
        ft_has = self.build_ft_has(monkeypatch, "")
        assert ft_has["ohlcv_has_history"] is True
        assert ft_has["ws_enabled"] is False


# =============================================================================
# Kraken trade-pagination method quirks (mirrored from freqtrade Kraken class)
# =============================================================================


@pytest.mark.skipif(not FREQTRADE_AVAILABLE, reason="freqtrade not installed")
class TestKrakenTradePagination:
    """
    The trades_pagination _ft_has flags need Kraken's two method overrides
    (_get_trade_pagination_next_value / _valid_trade_pagination_id) for
    id-based pagination to work. Nerdbot_Vault mirrors them for
    REAL_EXCHANGE=kraken only; the drift-guard tests compare behavior
    against the real Kraken class on the same sample inputs.
    """

    KRAKEN_CURSOR = "1705443695120072285"  # 19-char nanosecond timestamp id

    SAMPLE_TRADES = [
        # info is a raw Kraken trade list (>7 entries) - cursor is its tail.
        (
            [
                {
                    "info": ["p", "v", "t", "s", "o", "m", "l", "x", KRAKEN_CURSOR],
                    "timestamp": 1705443695120,
                }
            ],
            KRAKEN_CURSOR,
        ),
        # info too short - fall back to the timestamp.
        ([{"info": ["p", "v"], "timestamp": 1705443695120}], 1705443695120),
        # info not a list (dict) - fall back to the timestamp.
        ([{"info": {"last": "x"}, "timestamp": 1705443695120}], 1705443695120),
        # info missing entirely - fall back to the timestamp.
        ([{"timestamp": 1705443695120}], 1705443695120),
        # no trades at all.
        ([], None),
    ]

    @staticmethod
    def make_vault_exchange(monkeypatch, real_exchange: str):
        from user_data.exchange.nerdbot_vault import Nerdbot_Vault

        monkeypatch.setenv("REAL_EXCHANGE", real_exchange)
        exchange = object.__new__(Nerdbot_Vault)
        # Attributes Exchange.close()/__del__ dereference at GC time.
        exchange._exchange_ws = None
        exchange._ws_async = None
        exchange.loop = None
        return exchange

    @staticmethod
    def make_kraken():
        from freqtrade.exchange.kraken import Kraken

        kraken = object.__new__(Kraken)
        # Attributes Exchange.close()/__del__ dereference at GC time.
        kraken._exchange_ws = None
        kraken._ws_async = None
        kraken.loop = None
        return kraken

    @pytest.mark.parametrize(("trades", "expected"), SAMPLE_TRADES)
    def test_kraken_pagination_next_value(self, vault_env, monkeypatch, trades, expected):
        exchange = self.make_vault_exchange(monkeypatch, "kraken")
        assert exchange._get_trade_pagination_next_value(trades) == expected

    @pytest.mark.parametrize(("trades", "expected"), SAMPLE_TRADES)
    def test_kraken_pagination_next_value_matches_kraken_class(
        self, vault_env, monkeypatch, trades, expected
    ):
        # Drift guard: identical output to the real Kraken class.
        exchange = self.make_vault_exchange(monkeypatch, "kraken")
        assert exchange._get_trade_pagination_next_value(trades) == (
            self.make_kraken()._get_trade_pagination_next_value(trades)
        )

    @pytest.mark.parametrize(
        ("from_id", "expected"),
        [
            (KRAKEN_CURSOR, True),  # 19 chars - valid
            ("17054436951200722851", True),  # longer is fine too
            ("170544369512007228", False),  # 18 chars - invalid
            ("", False),
        ],
    )
    def test_kraken_valid_pagination_id(self, vault_env, monkeypatch, from_id, expected):
        exchange = self.make_vault_exchange(monkeypatch, "kraken")
        assert exchange._valid_trade_pagination_id("SOL/USD", from_id) is expected
        # Drift guard: identical to the real Kraken class.
        assert exchange._valid_trade_pagination_id("SOL/USD", from_id) is (
            self.make_kraken()._valid_trade_pagination_id("SOL/USD", from_id)
        )

    @pytest.mark.parametrize("real_exchange", ["binance", "coinbase"])
    def test_other_exchanges_keep_generic_behavior(self, vault_env, monkeypatch, real_exchange):
        exchange = self.make_vault_exchange(monkeypatch, real_exchange)
        # Generic Exchange consults _ft_has["trades_pagination"] ("time" for
        # these exchanges) and returns the timestamp - and any id passes.
        exchange._ft_has = {"trades_pagination": "time"}
        trades = [{"id": "abc", "timestamp": 1705443695120, "info": ["x"] * 9}]
        assert exchange._get_trade_pagination_next_value(trades) == 1705443695120
        assert exchange._valid_trade_pagination_id("SOL/USD", "short-id") is True


# =============================================================================
# Kraken dark-pool filter (market_is_tradable, mirrored from Kraken class)
# =============================================================================


@pytest.mark.skipif(not FREQTRADE_AVAILABLE, reason="freqtrade not installed")
class TestMarketIsTradable:
    """
    Kraken lists dark-pool pairs that must never be tradable; upstream
    filters them in Kraken.market_is_tradable. Nerdbot_Vault mirrors the
    filter for REAL_EXCHANGE=kraken only; a drift-guard test compares
    behavior against the real Kraken class on the same market dicts.
    """

    SPOT_MARKET = {
        "quote": "USD",
        "base": "SOL",
        "spot": True,
        "precision": {"price": 0.01},
    }

    SAMPLE_MARKETS = [
        SPOT_MARKET,  # plain tradable spot market
        {**SPOT_MARKET, "darkpool": False},  # explicit non-darkpool
        {**SPOT_MARKET, "darkpool": True},  # dark-pool pair
        {**SPOT_MARKET, "base": None},  # untradable regardless of darkpool
    ]

    @staticmethod
    def prepare(exchange):
        """Set the attributes generic market_is_tradable dereferences."""
        from freqtrade.enums import TradingMode

        exchange.trading_mode = TradingMode.SPOT
        # precisionMode is a property reading self._api.precisionMode;
        # 2 = ccxt DECIMAL_PLACES (not TICK_SIZE), so the precision branch
        # of the generic checks short-circuits deterministically.
        exchange._api = MagicMock(precisionMode=2)
        return exchange

    def make_vault_exchange(self, monkeypatch, real_exchange: str):
        return self.prepare(
            TestKrakenTradePagination.make_vault_exchange(monkeypatch, real_exchange)
        )

    def make_kraken(self):
        return self.prepare(TestKrakenTradePagination.make_kraken())

    def test_kraken_rejects_darkpool_markets(self, vault_env, monkeypatch):
        exchange = self.make_vault_exchange(monkeypatch, "kraken")
        assert exchange.market_is_tradable({**self.SPOT_MARKET, "darkpool": True}) is False

    def test_kraken_accepts_regular_markets(self, vault_env, monkeypatch):
        exchange = self.make_vault_exchange(monkeypatch, "kraken")
        assert exchange.market_is_tradable(dict(self.SPOT_MARKET)) is True
        assert exchange.market_is_tradable({**self.SPOT_MARKET, "darkpool": False}) is True

    @pytest.mark.parametrize("market", SAMPLE_MARKETS)
    def test_kraken_matches_kraken_class_source(self, vault_env, monkeypatch, market):
        # Drift guard: identical verdict to the real Kraken class.
        exchange = self.make_vault_exchange(monkeypatch, "kraken")
        assert exchange.market_is_tradable(dict(market)) is (
            self.make_kraken().market_is_tradable(dict(market))
        )

    @pytest.mark.parametrize("real_exchange", ["binance", "coinbase"])
    def test_other_exchanges_ignore_darkpool_flag(self, vault_env, monkeypatch, real_exchange):
        # The darkpool key is Kraken-specific; other exchanges keep the
        # generic verdict even if a market dict happens to carry it.
        exchange = self.make_vault_exchange(monkeypatch, real_exchange)
        assert exchange.market_is_tradable({**self.SPOT_MARKET, "darkpool": True}) is True

    def test_generic_untradable_stays_untradable_on_kraken(self, vault_env, monkeypatch):
        # The mirror only ANDs the darkpool filter onto the generic checks.
        exchange = self.make_vault_exchange(monkeypatch, "kraken")
        assert exchange.market_is_tradable({**self.SPOT_MARKET, "base": None}) is False


# =============================================================================
# ccxt shim registration robustness
# =============================================================================


class TestShimRegistrationGuard:
    def test_register_shim_survives_missing_exchanges_attribute(self, vault_env, monkeypatch):
        # Some ccxt versions do not expose an ``exchanges`` list on
        # ccxt.async_support; registration must not crash on AttributeError.
        import ccxt.async_support as ccxt_async

        monkeypatch.delattr(ccxt_async, "exchanges", raising=False)
        assert register_ccxt_shim() is True
        assert hasattr(ccxt_async, "nerdbot_vault")
        assert "nerdbot_vault" in ccxt.exchanges


# =============================================================================
# Freqtrade drop-in registration (only when freqtrade is installed)
# =============================================================================


@pytest.mark.skipif(not FREQTRADE_AVAILABLE, reason="freqtrade not installed")
class TestFreqtradeRegistration:
    def test_register_exposes_class_for_resolver(self, vault_env):
        import freqtrade.exchange as ft_exchange_pkg
        from freqtrade.exchange import Exchange

        assert register() is True
        # The resolver does: getattr(freqtrade.exchange, "nerdbot_vault".title())
        resolved = getattr(ft_exchange_pkg, "nerdbot_vault".title())
        assert issubclass(resolved, Exchange)

    def test_ccxt_shim_registered_without_credentials(self, vault_env):
        register()
        assert "nerdbot_vault" in ccxt.exchanges
        shim = ccxt.nerdbot_vault({"enableRateLimit": True})
        assert shim.id == "nerdbot_vault"
        assert not shim.apiKey and not shim.secret
        # Behaves as the real exchange for public data (subclass of binance)
        assert isinstance(shim, ccxt.binance)
