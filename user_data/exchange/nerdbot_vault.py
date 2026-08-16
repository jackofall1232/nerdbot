"""
nerdbot_vault - CCXT/Freqtrade exchange adapter backed by the nerdbot-vault
credential proxy.

Freqtrade believes it is talking to an exchange; in reality every
CREDENTIALED operation (place order, cancel order, query order state, list
open orders, fetch balances, validate credentials) is executed by the
nerdbot-vault proxy. Exchange API credentials NEVER reach this container.
Market data (tickers, OHLCV, order books, markets) is fetched directly from
the real exchange's PUBLIC endpoints via a zero-credential ccxt instance.

This module provides two layers:

1. ``NerdbotVaultAdapter`` - a ccxt-shaped adapter (create_order,
   cancel_order, fetch_balance, fetch_ticker, fetch_ohlcv, fetch_order,
   fetch_open_orders). It is fully testable standalone (no freqtrade import
   required) and contains all vault / paper-trading routing logic.

2. ``Nerdbot_Vault`` - a ``freqtrade.exchange.Exchange`` subclass that makes
   ``"exchange": {"name": "nerdbot_vault"}`` work as a drop-in with
   Freqtrade's resolver. Only defined when freqtrade is importable.

HOW FREQTRADE RESOLUTION WORKS (and why registration is needed)
----------------------------------------------------------------
``freqtrade.resolvers.exchange_resolver.ExchangeResolver`` takes
``config["exchange"]["name"]`` ("nerdbot_vault"), calls ``.title()`` on it
("Nerdbot_Vault") and looks the class up as an attribute of the
``freqtrade.exchange`` package - it does NOT scan user_data. Additionally,
``freqtrade.exchange.check_exchange`` and ``Exchange._init_ccxt`` require
the exchange id to be known to the ccxt library itself.

Since Freqtrade core must not be modified, ``register()`` (called on import
of this module) makes both work without touching core:

- registers a zero-credential ccxt shim class named ``nerdbot_vault``
  (subclassing the REAL_EXCHANGE ccxt class) into ``ccxt`` and
  ``ccxt.async_support`` and appends "nerdbot_vault" to their ``exchanges``
  lists. All public market-data calls made by Freqtrade through this shim
  hit the real exchange; private calls are impossible (credentials are
  stripped).
- injects ``Nerdbot_Vault`` into the ``freqtrade.exchange`` namespace so the
  resolver finds it.

This module must therefore be imported before Freqtrade boots. The shipped
``sitecustomize.py`` (this directory) does exactly that: ``start_bot.sh``
prepends ``user_data/exchange`` to PYTHONPATH, and Python imports
``sitecustomize`` automatically at interpreter startup - before
``freqtrade trade`` runs.

ENVIRONMENT VARIABLES
---------------------
- VAULT_BASE_URL        e.g. https://vault.internal:8443
- VAULT_KEY_ID          UUID of the vault key to proxy through
- BACKEND_TOKEN         backend bearer token (NEVER logged)
- BACKEND_INSTANCE_ID   identifier of this backend instance
- REAL_EXCHANGE         underlying exchange: binance | kraken | coinbase
- BOT_ID                UUID of this bot (lease binding)
- IS_PAPER_TRADING      optional; "1"/"true"/"yes"/"on" enables paper mode

SECURITY INVARIANTS
-------------------
- A FRESH lease is acquired before EVERY vault proxy call; leases are never
  cached or reused (vault TTL is ~30s; we treat each as single-use).
- Lease acquisition failure raises ``ccxt.AuthenticationError``.
- Paper mode never calls the vault for order placement/cancellation/state
  (orders are simulated and tracked locally; the vault rejects paper keys
  for live orders), but it still uses the vault for balance reads and
  startup credential validation. Vault configuration is therefore required
  in paper mode too (the backend always supplies the vault env vars).
- Live ``fetch_order`` / ``fetch_open_orders`` are delegated to the vault's
  read-only order-query endpoints (orders/query, orders/open) - never to
  public ccxt endpoints, which exchanges reject for private data.
- The market-data path holds zero credentials.
- The backend token is never logged.
"""

import logging
import os
import time


try:  # package-style import (tests / repo usage)
    from user_data.exchange.market_data_client import MarketDataClient
    from user_data.exchange.paper_trading import PaperTradingSimulator
    from user_data.exchange.vault_http_client import VaultHTTPClient
except ImportError:  # flat import (PYTHONPATH=user_data/exchange at runtime)
    from market_data_client import MarketDataClient  # type: ignore[no-redef]
    from paper_trading import PaperTradingSimulator  # type: ignore[no-redef]
    from vault_http_client import VaultHTTPClient  # type: ignore[no-redef]

import ccxt


logger = logging.getLogger(__name__)

SUPPORTED_REAL_EXCHANGES = ("binance", "kraken", "coinbase")

_TRUTHY = {"1", "true", "yes", "on"}

#: Default paper wallet when IS_PAPER_TRADING is enabled.
DEFAULT_PAPER_WALLET = {"USDT": 1000.0, "USD": 1000.0}

# Map vault order status -> ccxt order status (place-order responses)
_VAULT_ORDER_STATUS_TO_CCXT = {
    "open": "open",
    "filled": "closed",
    "rejected": "rejected",
}

# Map vault OrderEntry status -> ccxt order status (order-query responses).
# Note: CCXT uses the American spelling "canceled".
_VAULT_ENTRY_STATUS_TO_CCXT = {
    "open": "open",
    "closed": "closed",
    "cancelled": "canceled",
}


def _float_or_none(value) -> float | None:
    """Coerce vault decimal strings to float, passing None through."""
    return float(value) if value is not None else None


def _order_entry_to_ccxt(entry: dict, requested_symbol: str | None = None) -> dict:
    """
    Convert a vault OrderEntry dict to the standard CCXT order structure.

    The requested ccxt symbol (e.g. "SOL/USD") is preferred over the
    exchange-normalised symbol the vault echoes back (Kraken returns e.g.
    "SOLUSD"), so Freqtrade's pair bookkeeping keeps working.
    """
    order_id = entry.get("order_id")
    if not order_id:
        raise ccxt.ExchangeError("vault order entry missing order_id")
    status = entry.get("status")
    return {
        "id": str(order_id),
        "clientOrderId": None,
        "timestamp": None,
        "datetime": None,
        "symbol": requested_symbol or entry.get("symbol"),
        "type": entry.get("type"),
        "side": entry.get("side"),
        "price": _float_or_none(entry.get("price")),
        "average": _float_or_none(entry.get("avg_price")),
        "amount": _float_or_none(entry.get("amount")),
        "filled": float(entry.get("filled_amount") or 0.0),
        "remaining": _float_or_none(entry.get("remaining")),
        "status": _VAULT_ENTRY_STATUS_TO_CCXT.get(status, status),
        "fee": None,
        "trades": [],
        "info": entry,
    }


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Required environment variable {name} is not set")
    return value


class NerdbotVaultAdapter:
    """
    ccxt-shaped adapter routing credentialed operations through the vault.

    Constructor arguments exist only for dependency injection in tests;
    by default everything is built from environment variables.
    """

    def __init__(
        self,
        vault_client: VaultHTTPClient | None = None,
        market_client: MarketDataClient | None = None,
        paper_simulator: PaperTradingSimulator | None = None,
        validate_on_init: bool = True,
    ) -> None:
        self.vault_base_url = os.environ.get("VAULT_BASE_URL", "").strip()
        self.vault_key_id = _require_env("VAULT_KEY_ID")
        self.backend_instance_id = _require_env("BACKEND_INSTANCE_ID")
        self.bot_id = _require_env("BOT_ID")
        self.real_exchange = _require_env("REAL_EXCHANGE").lower()
        self.is_paper_trading = _env_flag("IS_PAPER_TRADING")

        if self.real_exchange not in SUPPORTED_REAL_EXCHANGES:
            raise ValueError(
                f"REAL_EXCHANGE must be one of {SUPPORTED_REAL_EXCHANGES}, "
                f"got '{self.real_exchange}'"
            )

        if vault_client is not None:
            self.vault_client = vault_client
        else:
            backend_token = _require_env("BACKEND_TOKEN")
            self.vault_client = VaultHTTPClient(
                vault_base_url=_require_env("VAULT_BASE_URL"),
                backend_token=backend_token,
                instance_id=self.backend_instance_id,
            )
            # Do not keep a module-level reference to the token.
            del backend_token

        self.market_client = market_client or MarketDataClient(self.real_exchange)

        if self.is_paper_trading:
            self.paper_simulator = paper_simulator or PaperTradingSimulator(
                dict(DEFAULT_PAPER_WALLET), self.market_client
            )
        else:
            self.paper_simulator = paper_simulator

        if validate_on_init:
            # Acquire an initial lease and validate the stored credentials.
            # Fails hard (ccxt.AuthenticationError) if anything is wrong.
            self._validate_credentials_on_startup()

    # ------------------------------------------------------------------
    # Lease & validation
    # ------------------------------------------------------------------

    def _fresh_lease(self) -> str:
        """
        Acquire a FRESH lease for exactly one proxy operation.

        NEVER cached: vault leases have a ~30s TTL and we deliberately treat
        them as single-use to keep the authorisation window minimal.

        :raises ccxt.AuthenticationError: if the lease cannot be acquired.
        """
        return self.vault_client.acquire_lease(self.vault_key_id, self.bot_id)

    def _validate_credentials_on_startup(self) -> None:
        lease_id = self._fresh_lease()
        result = self.vault_client.validate_credentials(
            self.vault_key_id, lease_id, self.bot_id, self.real_exchange
        )
        if result.get("status") != "valid":
            raise ccxt.AuthenticationError(
                f"Vault credential validation failed for exchange '{self.real_exchange}' "
                f"(status={result.get('status')!r})"
            )
        logger.info(
            "Vault credentials validated for exchange '%s' (paper_trading=%s)",
            self.real_exchange,
            self.is_paper_trading,
        )

    # ------------------------------------------------------------------
    # Credentialed operations -> vault proxy (or paper simulator)
    # ------------------------------------------------------------------

    def create_order(
        self,
        symbol: str,
        type: str,  # noqa: A002 - ccxt signature
        side: str,
        amount: float,
        price: float | None = None,
        params: dict | None = None,
    ) -> dict:
        """
        Place an order. Routed through the vault proxy - or, in paper mode,
        simulated locally WITHOUT any vault call (the vault rejects paper
        keys for live orders).
        """
        params = params or {}
        if self.is_paper_trading:
            # SECURITY: never send paper orders to the vault.
            return self.paper_simulator.simulate_order(symbol, type, side, amount, price)

        lease_id = self._fresh_lease()
        response = self.vault_client.place_order(
            vault_key_id=self.vault_key_id,
            lease_id=lease_id,
            bot_id=self.bot_id,
            exchange=self.real_exchange,
            symbol=symbol,
            side=side,
            type=type,
            amount=amount,
            price=price,
            client_order_id=params.get("clientOrderId") or params.get("client_order_id"),
        )

        status = _VAULT_ORDER_STATUS_TO_CCXT.get(response.get("status"), "open")
        if status == "rejected":
            raise ccxt.InvalidOrder(
                f"Vault/exchange rejected {type} {side} order for {symbol} "
                f"(order_id={response.get('order_id')!r})"
            )

        order_id = response.get("order_id")
        if not order_id:
            # Do NOT silently return an empty id: the vault accepted the
            # request, so an order may have been placed on the exchange.
            # The caller must see an explicit error and reconcile.
            raise ccxt.ExchangeError("vault response missing order_id")

        filled = float(response.get("filled_amount") or 0.0)
        avg_price = response.get("avg_price")
        average = float(avg_price) if avg_price is not None else None
        amount = float(amount)
        now_ms = int(time.time() * 1000)
        return {
            "id": str(order_id),
            "clientOrderId": params.get("clientOrderId"),
            "timestamp": now_ms,
            "datetime": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now_ms / 1000)),
            "lastTradeTimestamp": now_ms if filled else None,
            "symbol": symbol,
            "type": type,
            "timeInForce": "GTC",
            "side": side,
            "price": float(price) if price is not None else average,
            "average": average,
            "amount": amount,
            "filled": filled,
            "remaining": max(amount - filled, 0.0),
            "cost": (filled * average) if average is not None else 0.0,
            "status": status,
            "fee": None,
            "trades": [],
            "info": response,
        }

    def cancel_order(self, id: str, symbol: str | None = None, params: dict | None = None) -> dict:  # noqa: A002
        """Cancel an order via the vault proxy (or paper simulator)."""
        if self.is_paper_trading:
            return self.paper_simulator.cancel_order(id)

        lease_id = self._fresh_lease()
        response = self.vault_client.cancel_order(
            vault_key_id=self.vault_key_id,
            lease_id=lease_id,
            bot_id=self.bot_id,
            exchange=self.real_exchange,
            order_id=id,
        )
        status = response.get("status")
        if status == "not_found":
            raise ccxt.OrderNotFound(f"Order {id} not found on cancel")
        if status == "rejected":
            raise ccxt.InvalidOrder(f"Cancel of order {id} was rejected")
        return {
            "id": str(id),
            "symbol": symbol,
            "status": "canceled",
            "filled": 0.0,
            "fee": {},
            "info": response,
        }

    def fetch_balance(self, params: dict | None = None) -> dict:
        """
        Fetch balances via the vault proxy (or paper simulator) and convert
        to the standard CCXT balance structure.
        """
        if self.is_paper_trading:
            return self.paper_simulator.get_balance()

        lease_id = self._fresh_lease()
        response = self.vault_client.get_balances(
            vault_key_id=self.vault_key_id,
            lease_id=lease_id,
            bot_id=self.bot_id,
            exchange=self.real_exchange,
        )
        result: dict = {"info": response, "free": {}, "used": {}, "total": {}}
        for entry in response.get("balances", []):
            asset = entry.get("asset")
            if not asset:
                # Required field per the vault contract; a malformed entry
                # must surface as an explicit error, not a KeyError.
                raise ccxt.ExchangeError("vault balance entry missing asset")
            free = float(entry.get("available") or 0.0)
            used = float(entry.get("locked") or 0.0)
            total = free + used
            result[asset] = {"free": free, "used": used, "total": total}
            result["free"][asset] = free
            result["used"][asset] = used
            result["total"][asset] = total
        return result

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """
        Release the vault HTTP client's connection pool.

        Idempotent: safe to call multiple times.
        """
        self.vault_client.close()

    # ------------------------------------------------------------------
    # Market data -> read-only ccxt (zero credentials)
    # ------------------------------------------------------------------

    def fetch_ticker(self, symbol: str, params: dict | None = None) -> dict:
        return self.market_client.fetch_ticker(symbol)

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1m",
        since: int | None = None,
        limit: int = 500,
        params: dict | None = None,
    ) -> list:
        return self.market_client.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)

    # ------------------------------------------------------------------
    # Order state (read-only) -> vault proxy (or paper simulator)
    # ------------------------------------------------------------------

    def fetch_order(self, id: str, symbol: str | None = None, params: dict | None = None) -> dict:  # noqa: A002
        """
        Fetch the current state of an order.

        Live mode: delegated to the vault's read-only orders/query proxy
        endpoint (fresh lease per call). Paper mode: served from the local
        simulator - the vault is never called.
        """
        if self.is_paper_trading:
            return self.paper_simulator.get_order(id)

        lease_id = self._fresh_lease()
        entry = self.vault_client.query_order(
            vault_key_id=self.vault_key_id,
            bot_id=self.bot_id,
            lease_id=lease_id,
            order_id=id,
            exchange=self.real_exchange,
            symbol=symbol,
        )
        return _order_entry_to_ccxt(entry, requested_symbol=symbol)

    def fetch_open_orders(self, symbol: str | None = None, params: dict | None = None) -> list:
        """
        List currently open orders.

        Live mode: delegated to the vault's read-only orders/open proxy
        endpoint (fresh lease per call). Paper mode: served from the local
        simulator - the vault is never called.
        """
        if self.is_paper_trading:
            return self.paper_simulator.get_open_orders(symbol)

        lease_id = self._fresh_lease()
        entries = self.vault_client.get_open_orders(
            vault_key_id=self.vault_key_id,
            bot_id=self.bot_id,
            lease_id=lease_id,
            exchange=self.real_exchange,
            symbol=symbol,
        )
        return [_order_entry_to_ccxt(entry, requested_symbol=symbol) for entry in entries]


# =============================================================================
# ccxt shim + Freqtrade Exchange subclass (drop-in resolution)
# =============================================================================

try:
    from copy import deepcopy

    from freqtrade.exceptions import (
        DDosProtection,
        InsufficientFundsError,
        InvalidOrderException,
        OperationalException,
        RetryableOrderError,
        TemporaryError,
    )
    from freqtrade.exchange import Exchange as _FreqtradeExchange
    from freqtrade.exchange.common import API_FETCH_ORDER_RETRY_COUNT, retrier
    from freqtrade.misc import deep_merge_dicts

    FREQTRADE_AVAILABLE = True
except ImportError:  # freqtrade (or its dependencies) not installed
    _FreqtradeExchange = object  # type: ignore[assignment, misc]
    FREQTRADE_AVAILABLE = False


def _make_ccxt_shim(base_class):
    """
    Build a ccxt exchange class with id 'nerdbot_vault' that behaves exactly
    like the REAL_EXCHANGE ccxt class for PUBLIC endpoints. It is only ever
    instantiated without credentials.
    """

    class nerdbot_vault(base_class):
        def describe(self):
            return self.deep_extend(
                super().describe(),
                {"id": "nerdbot_vault", "name": "NerdbotVault"},
            )

    return nerdbot_vault


def register_ccxt_shim(real_exchange: str | None = None) -> bool:
    """
    Register the 'nerdbot_vault' exchange id with ccxt (sync + async).

    Required so that Freqtrade's ``check_exchange`` and
    ``Exchange._init_ccxt`` accept "nerdbot_vault" as an exchange name.
    """
    real_exchange = (real_exchange or os.environ.get("REAL_EXCHANGE", "")).lower().strip()
    if not real_exchange:
        logger.warning("REAL_EXCHANGE not set - skipping ccxt shim registration")
        return False
    if real_exchange not in SUPPORTED_REAL_EXCHANGES:
        logger.warning("REAL_EXCHANGE '%s' unsupported - skipping shim", real_exchange)
        return False

    import ccxt.async_support as ccxt_async

    for module in (ccxt, ccxt_async):
        base_class = getattr(module, real_exchange)
        shim = _make_ccxt_shim(base_class)
        module.nerdbot_vault = shim
        # Some ccxt versions do not expose an ``exchanges`` list on every
        # module (notably ccxt.async_support); guard so registration never
        # crashes interpreter startup with an AttributeError.
        if hasattr(module, "exchanges") and "nerdbot_vault" not in module.exchanges:
            module.exchanges.append("nerdbot_vault")
    # NOTE: deliberately NOT registered with ccxt.pro - Freqtrade falls back
    # to ccxt.async_support, and websockets are disabled via ft_has below.
    return True


if FREQTRADE_AVAILABLE:
    # Data-correctness ``_ft_has`` quirks per REAL_EXCHANGE, mirrored from the
    # corresponding freqtrade exchange subclasses (freqtrade/exchange/kraken.py).
    # ``Nerdbot_Vault`` extends the GENERIC Exchange class, so the per-exchange
    # subclasses' quirks would otherwise be silently lost even though market
    # data flows through the real exchange's public API. Only entries that
    # affect market DATA (candle fetching, pairlists, public trade downloads)
    # are mirrored - order-execution quirks (``stoploss_on_exchange``, stop
    # price params, ``order_time_in_force``) are deliberately NOT brought
    # over, since orders route through Freqtrade dry-run or the vault proxy,
    # never through native exchange order endpoints.
    REAL_EXCHANGE_DATA_FT_HAS: dict[str, dict] = {
        "kraken": {
            # Kraken's OHLCV endpoint only serves the most recent ~720
            # candles - historic candle downloads must be trade-based.
            "ohlcv_has_history": False,
            # Public trade-history pagination works by id, not by time.
            "trades_pagination": "id",
            "trades_pagination_arg": "since",
            "trades_pagination_overlap": False,
            "trades_has_history": True,
        },
        # binance / coinbase: the generic Exchange defaults are data-correct.
        "binance": {},
        "coinbase": {},
    }

    class Nerdbot_Vault(_FreqtradeExchange):
        """
        Freqtrade Exchange subclass for ``"exchange": {"name": "nerdbot_vault"}``.

        Market data flows through the inherited Exchange machinery using the
        credential-free ccxt shim (real exchange public API). Credentialed
        operations (create_order, cancel_order, fetch_order,
        fetch_open_orders, get_balances) are overridden to go through the
        vault proxy via ``NerdbotVaultAdapter``. With ``dry_run: true``
        (paper configs), the vault is never called for order
        placement/cancellation/state - Freqtrade's own local simulation
        handles and tracks orders - but ``get_balances`` still reads real
        balances through the vault, and startup credential validation always
        runs. Vault configuration is therefore required in paper mode too.
        """

        _ft_has: dict = {
            "ws_enabled": False,  # no ccxt.pro class is registered for the shim
        }

        @classmethod
        def combine_ft_has(cls, include_futures: bool) -> dict:
            """
            Parent combination (class ``_ft_has`` over defaults), then merge
            the REAL_EXCHANGE data-correctness quirks on top. Config-level
            ``_ft_has_params`` overrides are applied afterwards by
            ``build_ft_has`` and therefore still win. ``ws_enabled`` stays
            False for every real exchange (set in ``_ft_has`` above, never
            overridden by ``REAL_EXCHANGE_DATA_FT_HAS``).
            """
            ft_has = super().combine_ft_has(include_futures)
            real_exchange = os.environ.get("REAL_EXCHANGE", "").strip().lower()
            quirks = REAL_EXCHANGE_DATA_FT_HAS.get(real_exchange)
            if quirks:
                ft_has = deep_merge_dicts(deepcopy(quirks), ft_has)
            return ft_has

        @staticmethod
        def _current_real_exchange() -> str:
            """REAL_EXCHANGE env, normalized (same source combine_ft_has uses)."""
            return os.environ.get("REAL_EXCHANGE", "").strip().lower()

        # --------------------------------------------------------------
        # Kraken trade-pagination method quirks (data path)
        #
        # The trades_pagination _ft_has flags mirrored above depend on two
        # Kraken method overrides for id-based pagination to actually work
        # (--dl-trades / trade-based candle downloads). Both are mirrored
        # verbatim from freqtrade/exchange/kraken.py (Kraken class) and are
        # active ONLY when REAL_EXCHANGE=kraken; every other exchange keeps
        # the generic Exchange behavior. A drift-guard test compares this
        # logic against the real Kraken class on sample inputs
        # (tests/test_vault_adapter.py::TestKrakenTradePagination).
        # --------------------------------------------------------------

        def _get_trade_pagination_next_value(self, trades: list[dict]):
            """
            Extract the next "from_id" pagination value.

            Kraken: the cursor is the trade response's "last" value, found
            at the end of the raw info list; fall back to the timestamp
            when info is somehow empty (mirrors Kraken class).
            """
            if self._current_real_exchange() != "kraken":
                return super()._get_trade_pagination_next_value(trades)
            if len(trades) > 0:
                if isinstance(trades[-1].get("info"), list) and len(trades[-1].get("info", [])) > 7:
                    # Trade response's "last" value.
                    return trades[-1].get("info", [])[-1]
                # Fall back to timestamp if info is somehow empty.
                return trades[-1].get("timestamp")
            return None

        def _valid_trade_pagination_id(self, pair: str, from_id: str) -> bool:
            """
            Verify a trade-pagination id is valid.

            Kraken: regular ids are 19+ char nanosecond timestamps (e.g.
            1705443695120072285); shorter ids are invalid and force the
            timestamp fallback (mirrors Kraken class).
            """
            if self._current_real_exchange() != "kraken":
                return super()._valid_trade_pagination_id(pair, from_id)
            if len(from_id) >= 19:
                return True
            logger.debug("%s - trade-pagination id is not valid. Fallback to timestamp.", pair)
            return False

        def __init__(
            self, config, *, exchange_config=None, validate=True, load_leverage_tiers=False
        ) -> None:
            # Build the vault adapter first: acquires an initial lease and
            # validates credentials - fail hard BEFORE the bot starts.
            self._vault_adapter = NerdbotVaultAdapter()
            super().__init__(
                config,
                exchange_config=exchange_config,
                validate=validate,
                load_leverage_tiers=load_leverage_tiers,
            )

        def _init_ccxt(self, exchange_config, sync, ccxt_kwargs):
            """
            Initialize the underlying ccxt shim with ALL credentials
            stripped, regardless of what is present in the config file.
            """
            sanitized = dict(exchange_config)
            for credential_key in (
                "key",
                "apiKey",
                "api_key",
                "secret",
                "password",
                "uid",
                "account_id",
                "accountId",
                "wallet_address",
                "walletAddress",
                "private_key",
                "privateKey",
            ):
                sanitized.pop(credential_key, None)
            return super()._init_ccxt(sanitized, sync, ccxt_kwargs)

        # --------------------------------------------------------------
        # Credentialed operations -> vault
        # --------------------------------------------------------------

        def create_order(
            self,
            *,
            pair: str,
            ordertype: str,
            side,
            amount: float,
            rate: float,
            leverage: float,
            time_in_force: str = "GTC",
            reduceOnly: bool = False,
            initial_order: bool = True,
        ):
            if self._config["dry_run"]:
                # Paper trading: local Freqtrade simulation; vault NOT called.
                return super().create_order(
                    pair=pair,
                    ordertype=ordertype,
                    side=side,
                    amount=amount,
                    rate=rate,
                    leverage=leverage,
                    time_in_force=time_in_force,
                    reduceOnly=reduceOnly,
                    initial_order=initial_order,
                )
            try:
                amount = self.amount_to_precision(pair, self._amount_to_contracts(pair, amount))
                needs_price = self._order_needs_price(side, ordertype)
                rate_for_order = self.price_to_precision(pair, rate) if needs_price else None

                order = self._vault_adapter.create_order(
                    pair, ordertype, side, amount, rate_for_order
                )
                if order.get("type") is None:
                    order["type"] = ordertype
                self._log_exchange_response("create_order", order)
                return self._order_contracts_to_amount(order)
            except ccxt.InsufficientFunds as e:
                raise InsufficientFundsError(
                    f"Insufficient funds to create {ordertype} {side} order on market {pair}. "
                    f"Message: {e}"
                ) from e
            except ccxt.InvalidOrder as e:
                raise InvalidOrderException(
                    f"Could not create {ordertype} {side} order on market {pair}. Message: {e}"
                ) from e
            except ccxt.DDoSProtection as e:
                raise DDosProtection(e) from e
            except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
                raise TemporaryError(
                    f"Could not place {side} order due to {e.__class__.__name__}. Message: {e}"
                ) from e
            except ccxt.BaseError as e:
                raise OperationalException(e) from e

        def cancel_order(self, order_id: str, pair: str, params: dict | None = None):
            if self._config["dry_run"]:
                return super().cancel_order(order_id, pair, params)
            try:
                order = self._vault_adapter.cancel_order(order_id, pair)
                order.setdefault("amount", 0.0)
                self._log_exchange_response("cancel_order", order)
                return order
            except ccxt.InvalidOrder as e:
                raise InvalidOrderException(f"Could not cancel order. Message: {e}") from e
            except ccxt.DDoSProtection as e:
                raise DDosProtection(e) from e
            except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
                raise TemporaryError(
                    f"Could not cancel order due to {e.__class__.__name__}. Message: {e}"
                ) from e
            except ccxt.BaseError as e:
                raise OperationalException(e) from e

        @retrier(retries=API_FETCH_ORDER_RETRY_COUNT)
        def fetch_order(self, order_id: str, pair: str, params: dict | None = None):
            if self._config["dry_run"]:
                # Paper trading: Freqtrade's local dry-run order bookkeeping;
                # vault NOT called.
                return super().fetch_order(order_id, pair, params)
            try:
                order = self._vault_adapter.fetch_order(order_id, pair)
                self._log_exchange_response("fetch_order", order)
                return self._order_contracts_to_amount(order)
            except ccxt.OrderNotFound as e:
                raise RetryableOrderError(
                    f"Order not found (pair: {pair} id: {order_id}). Message: {e}"
                ) from e
            except ccxt.InvalidOrder as e:
                raise InvalidOrderException(
                    f"Tried to get an invalid order (pair: {pair} id: {order_id}). Message: {e}"
                ) from e
            except ccxt.DDoSProtection as e:
                raise DDosProtection(e) from e
            except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
                raise TemporaryError(
                    f"Could not get order due to {e.__class__.__name__}. Message: {e}"
                ) from e
            except ccxt.BaseError as e:
                raise OperationalException(e) from e

        def fetch_open_orders(self, pair: str | None = None, params: dict | None = None) -> list:
            if self._config["dry_run"]:
                # Paper trading: Freqtrade's local dry-run order bookkeeping;
                # vault NOT called. (The base Exchange class has no public
                # fetch_open_orders, so there is no super() to defer to.)
                return [
                    order
                    for order in self._dry_run_open_orders.values()
                    if order.get("status") == "open"
                    and (pair is None or order.get("symbol") == pair)
                ]
            try:
                orders = self._vault_adapter.fetch_open_orders(pair)
                self._log_exchange_response("fetch_open_orders", orders)
                return [self._order_contracts_to_amount(order) for order in orders]
            except ccxt.DDoSProtection as e:
                raise DDosProtection(e) from e
            except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
                raise TemporaryError(
                    f"Could not get open orders due to {e.__class__.__name__}. Message: {e}"
                ) from e
            except ccxt.BaseError as e:
                raise OperationalException(e) from e

        def close(self):
            """Close the vault adapter's HTTP client, then Freqtrade's own resources."""
            try:
                self._vault_adapter.close()
            except Exception:
                logger.exception("Failed to close vault adapter HTTP client")
            super().close()

        def get_balances(self, params: dict | None = None):
            try:
                balances = self._vault_adapter.fetch_balance(params or {})
                # Match Freqtrade's get_balances post-processing.
                balances.pop("info", None)
                balances.pop("free", None)
                balances.pop("total", None)
                balances.pop("used", None)
                self._log_exchange_response("fetch_balance", balances, add_info=params)
                return balances
            except ccxt.DDoSProtection as e:
                raise DDosProtection(e) from e
            except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
                raise TemporaryError(
                    f"Could not get balance due to {e.__class__.__name__}. Message: {e}"
                ) from e
            except ccxt.BaseError as e:
                raise OperationalException(e) from e

else:  # pragma: no cover - exercised only without freqtrade installed
    Nerdbot_Vault = None  # type: ignore[assignment]


def register() -> bool:
    """
    Make ``"exchange": {"name": "nerdbot_vault"}`` resolvable by Freqtrade.

    1. Registers the ccxt shim id 'nerdbot_vault' (needs REAL_EXCHANGE).
    2. Injects ``Nerdbot_Vault`` into the ``freqtrade.exchange`` namespace
       (the resolver looks classes up there via ``getattr``).

    Returns True if the Freqtrade class was registered.
    """
    register_ccxt_shim()
    if not FREQTRADE_AVAILABLE:
        logger.warning("freqtrade not importable - Nerdbot_Vault class not registered")
        return False
    import freqtrade.exchange as ft_exchange_pkg

    ft_exchange_pkg.Nerdbot_Vault = Nerdbot_Vault
    logger.info("Registered Nerdbot_Vault exchange with Freqtrade resolver")
    return True


# Register on import (sitecustomize imports this module at interpreter
# startup when launched via start_bot.sh). Never raise on import.
try:
    register()
except Exception:
    logger.exception("nerdbot_vault registration failed")
