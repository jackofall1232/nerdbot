"""
Tests for the nerdbot_vault adapter and the vault HTTP client.

These tests are network-free: the vault client is mocked (or backed by an
httpx.MockTransport) and market data clients are MagicMocks. They are also
runnable without freqtrade installed - the adapter core has no freqtrade
dependency.
"""

import json
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

        client.place_order.assert_called_once_with(
            vault_key_id=VAULT_KEY_ID,
            lease_id="lease-2",  # fresh lease, not the init lease
            bot_id=BOT_ID,
            exchange="binance",
            symbol="SOL/USDT",
            side="buy",
            type="limit",
            amount=2.0,
            price=100.0,
            client_order_id=None,
        )
        assert order["id"] == "EX-1"
        assert order["status"] == "open"
        assert order["symbol"] == "SOL/USDT"
        assert order["amount"] == 2.0
        assert order["remaining"] == 2.0

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
        adapter, _client = paper_adapter
        order = adapter.create_order("SOL/USDT", "limit", "buy", 1.0, 50.0)
        open_orders = adapter.fetch_open_orders("SOL/USDT")
        assert [o["id"] for o in open_orders] == [order["id"]]


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

    @pytest.mark.parametrize("status_code", [401, 403])
    def test_auth_rejection_raises_authentication_error(self, status_code):
        client = make_http_client(lambda request: httpx.Response(status_code, json={}))
        with pytest.raises(ccxt.AuthenticationError):
            client.get_balances(VAULT_KEY_ID, "lease-1", BOT_ID, "binance")

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
