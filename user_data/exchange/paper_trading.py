"""
In-memory paper-trading simulator.

Used when IS_PAPER_TRADING is enabled: orders are NEVER sent to the vault
(the vault rejects paper keys for live orders). Instead, fills are simulated
locally against REAL market prices fetched through the zero-credential
MarketDataClient.

NOTE: paper mode does not avoid the vault entirely. It never calls the
vault for order placement/cancellation (orders are simulated here), but the
adapter stack still uses the vault for balance reads and startup credential
validation - vault configuration is therefore required in paper mode too.
"""

import logging
import time
import uuid

import ccxt


logger = logging.getLogger(__name__)

#: Simulated taker/maker fee rate (0.1%, typical spot fee).
DEFAULT_FEE_RATE = 0.001


class PaperTradingSimulator:
    """
    Simulates order execution and balance accounting in memory.

    :param initial_wallet: e.g. {"USDT": 1000.0} or {"USDT": 500, "SOL": 2}
    :param market_client: MarketDataClient used to obtain real market prices
                          for realistic fills.
    """

    def __init__(self, initial_wallet: dict, market_client, fee_rate: float = DEFAULT_FEE_RATE):
        self._market_client = market_client
        self._fee_rate = fee_rate
        # currency -> {"free": float, "used": float}
        self._wallet: dict[str, dict[str, float]] = {
            currency: {"free": float(amount), "used": 0.0}
            for currency, amount in (initial_wallet or {}).items()
        }
        # order_id -> ccxt-format order dict
        self._orders: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_currency(self, currency: str) -> dict[str, float]:
        return self._wallet.setdefault(currency, {"free": 0.0, "used": 0.0})

    def _market_price(self, symbol: str) -> float:
        ticker = self._market_client.fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close") or ticker.get("bid") or ticker.get("ask")
        if not price:
            raise ccxt.ExchangeError(f"No market price available for {symbol}")
        return float(price)

    @staticmethod
    def _split_symbol(symbol: str) -> tuple[str, str]:
        if not symbol or not isinstance(symbol, str):
            raise ccxt.BadSymbol("Symbol is empty or None")
        try:
            base, quote = symbol.split("/", 1)
        except ValueError as exc:
            raise ccxt.BadSymbol(f"Invalid symbol '{symbol}' (expected BASE/QUOTE)") from exc
        # Strip settle-suffix if present (e.g. BTC/USDT:USDT)
        return base, quote.split(":", 1)[0]

    def _build_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None,
        status: str,
        fill_price: float | None,
        fee_cost: float,
        fee_currency: str,
    ) -> dict:
        order_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        filled = amount if status == "closed" else 0.0
        cost = (filled * fill_price) if (status == "closed" and fill_price) else 0.0
        order = {
            "id": order_id,
            "clientOrderId": order_id,
            "timestamp": now_ms,
            "datetime": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now_ms / 1000)),
            "lastTradeTimestamp": now_ms if status == "closed" else None,
            "symbol": symbol,
            "type": order_type,
            "timeInForce": "GTC",
            "side": side,
            "price": price if price is not None else fill_price,
            "average": fill_price if status == "closed" else None,
            "amount": amount,
            "filled": filled,
            "remaining": amount - filled,
            "cost": cost,
            "status": status,
            "fee": {"cost": fee_cost, "currency": fee_currency, "rate": self._fee_rate},
            "trades": [],
            "info": {"paper_trading": True},
        }
        self._orders[order_id] = order
        return order

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def simulate_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None = None,
    ) -> dict:
        """
        Simulate placing an order. Returns a CCXT-format order dict.

        Market orders (and immediately-marketable limit orders) fill at the
        REAL current market price. Non-marketable limit orders rest open and
        reserve funds.
        """
        if side not in ("buy", "sell"):
            raise ccxt.InvalidOrder(f"Invalid side '{side}'")
        if order_type not in ("market", "limit"):
            raise ccxt.InvalidOrder(f"Invalid order type '{order_type}'")
        if order_type == "limit" and price is None:
            raise ccxt.InvalidOrder("Limit orders require a price")
        amount = float(amount)
        if amount <= 0:
            raise ccxt.InvalidOrder("Order amount must be positive")

        base, quote = self._split_symbol(symbol)
        market_price = self._market_price(symbol)

        # Determine whether the order fills immediately - and at what price.
        if order_type == "market":
            fills_now = True
            fill_price = market_price
        else:
            price = float(price)
            # Marketable limit orders fill at the limit price (conservative).
            fills_now = (side == "buy" and price >= market_price) or (
                side == "sell" and price <= market_price
            )
            fill_price = price

        base_acct = self._ensure_currency(base)
        quote_acct = self._ensure_currency(quote)

        if side == "buy":
            ref_price = fill_price if order_type == "market" or fills_now else float(price)
            cost = amount * ref_price
            fee_cost = cost * self._fee_rate
            required = cost + fee_cost
            if quote_acct["free"] < required:
                raise ccxt.InsufficientFunds(
                    f"Paper wallet has {quote_acct['free']} {quote}, needs {required}"
                )
            if fills_now:
                quote_acct["free"] -= required
                base_acct["free"] += amount
                return self._build_order(
                    symbol, order_type, side, amount, price, "closed", fill_price, fee_cost, quote
                )
            # Resting limit buy: reserve quote funds.
            quote_acct["free"] -= required
            quote_acct["used"] += required
            return self._build_order(
                symbol, order_type, side, amount, price, "open", None, 0.0, quote
            )

        # side == "sell"
        if base_acct["free"] < amount:
            raise ccxt.InsufficientFunds(
                f"Paper wallet has {base_acct['free']} {base}, needs {amount}"
            )
        if fills_now:
            proceeds = amount * fill_price
            fee_cost = proceeds * self._fee_rate
            base_acct["free"] -= amount
            quote_acct["free"] += proceeds - fee_cost
            return self._build_order(
                symbol, order_type, side, amount, price, "closed", fill_price, fee_cost, quote
            )
        # Resting limit sell: reserve base funds.
        base_acct["free"] -= amount
        base_acct["used"] += amount
        return self._build_order(symbol, order_type, side, amount, price, "open", None, 0.0, quote)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a resting simulated order, releasing reserved funds."""
        order = self._orders.get(order_id)
        if order is None:
            raise ccxt.OrderNotFound(f"Paper order {order_id} not found")
        if order["status"] != "open":
            return order
        base, quote = self._split_symbol(order["symbol"])
        if order["side"] == "buy":
            reserved = order["amount"] * float(order["price"]) * (1 + self._fee_rate)
            acct = self._ensure_currency(quote)
            acct["used"] -= reserved
            acct["free"] += reserved
        else:
            acct = self._ensure_currency(base)
            acct["used"] -= order["amount"]
            acct["free"] += order["amount"]
        order["status"] = "canceled"
        order["remaining"] = order["amount"]
        return order

    def get_order(self, order_id: str) -> dict:
        order = self._orders.get(order_id)
        if order is None:
            raise ccxt.OrderNotFound(f"Paper order {order_id} not found")
        return order

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        """All currently open (resting) simulated orders."""
        return [
            order
            for order in self._orders.values()
            if order["status"] == "open" and (symbol is None or order["symbol"] == symbol)
        ]

    def get_balance(self) -> dict:
        """Current simulated balances in CCXT fetch_balance format."""
        result: dict = {"info": {"paper_trading": True}, "free": {}, "used": {}, "total": {}}
        for currency, acct in self._wallet.items():
            free = acct["free"]
            used = acct["used"]
            total = free + used
            result[currency] = {"free": free, "used": used, "total": total}
            result["free"][currency] = free
            result["used"][currency] = used
            result["total"][currency] = total
        return result
