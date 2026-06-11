"""
Synchronous HTTP client for the nerdbot-vault credential proxy.

The vault executes exchange API calls on our behalf so that exchange
credentials NEVER reach this container. All endpoints are POST-only and
require:

- ``Authorization: Bearer <BACKEND_TOKEN>``      (backend service auth)
- ``X-Backend-Instance-ID: <instance id>``       (instance binding)

Proxy operations (validate / orders / orders/cancel / balances) additionally
require a short-lived lease (~30s TTL), created via the lease endpoint and
passed in the ``X-Vault-Lease`` header. Leases are bound to
(vault_key_id, bot_id, backend_instance_id) by the vault.

Vault API contract (see nerdbot-vault app/routers/proxy.py and
app/schemas/proxy_api.py):

- POST /v1/proxy/{vault_key_id}/lease
    body: {"bot_id", "backend_instance_id", "lease_ttl_seconds"}
    resp: {"lease_id", "vault_key_id", "bot_id", "backend_instance_id",
           "expires_at"}
- POST /v1/proxy/{vault_key_id}/validate
    body: {"bot_id", "exchange"}
    resp: {"status": "valid"|"invalid", "permissions": {...}|null}
- POST /v1/proxy/{vault_key_id}/orders
    body: {"bot_id", "exchange", "symbol", "side", "type", "amount",
           "price"?, "client_order_id"?}
    resp: {"order_id", "status": "open"|"filled"|"rejected",
           "filled_amount", "avg_price"}
- POST /v1/proxy/{vault_key_id}/orders/cancel
    body: {"bot_id", "exchange", "order_id"}
    resp: {"status": "cancelled"|"not_found"|"rejected"}
- POST /v1/proxy/{vault_key_id}/balances
    body: {"bot_id", "exchange"}
    resp: {"balances": [{"asset", "available", "locked"}, ...]}

SECURITY: the backend token is never logged, never included in __repr__,
and never present in raised exception messages.
"""

import logging

import ccxt
import httpx


logger = logging.getLogger(__name__)

#: Default request timeout for all vault calls (seconds).
DEFAULT_TIMEOUT = 30.0

#: Default lease TTL requested from the vault (seconds). Vault allows 5-300.
DEFAULT_LEASE_TTL_SECONDS = 30


class VaultHTTPClient:
    """
    Thin, synchronous httpx wrapper around the vault proxy API.

    This class is intentionally dumb: it does not cache leases and holds no
    trading state. Lease lifecycle policy (fresh lease per operation) is
    enforced by the adapter layer.
    """

    def __init__(self, vault_base_url: str, backend_token: str, instance_id: str) -> None:
        if not vault_base_url:
            raise ValueError("vault_base_url is required")
        if not backend_token:
            raise ValueError("backend_token is required")
        if not instance_id:
            raise ValueError("instance_id is required")

        self._base_url = vault_base_url.rstrip("/")
        # Token kept private; never logged, never repr'd.
        self._backend_token = backend_token
        self._instance_id = instance_id
        self._client = httpx.Client(timeout=DEFAULT_TIMEOUT)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        # Deliberately omits the backend token.
        return f"VaultHTTPClient(base_url={self._base_url!r}, instance_id={self._instance_id!r})"

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _headers(self, lease_id: str | None = None) -> dict[str, str]:
        """
        Build request headers per the vault contract.

        :param lease_id: Lease id for proxy operations (X-Vault-Lease).
                         Omitted for the lease-creation endpoint itself.
        """
        headers = {
            "Authorization": f"Bearer {self._backend_token}",
            "X-Backend-Instance-ID": self._instance_id,
            "Content-Type": "application/json",
        }
        if lease_id is not None:
            headers["X-Vault-Lease"] = str(lease_id)
        return headers

    def _post(self, path: str, json_body: dict, lease_id: str | None = None) -> dict:
        """
        POST to the vault and translate failures into ccxt exceptions.

        Exception mapping (so the Freqtrade/ccxt layers above behave
        naturally):
          - network / timeout errors        -> ccxt.NetworkError
          - 401 / 403 (auth, lease invalid) -> ccxt.AuthenticationError
          - 429 (rate limited)              -> ccxt.RateLimitExceeded
          - other 4xx                       -> ccxt.ExchangeError
          - 5xx                             -> ccxt.ExchangeNotAvailable
        """
        url = f"{self._base_url}{path}"
        try:
            response = self._client.post(url, json=json_body, headers=self._headers(lease_id))
        except httpx.HTTPError as exc:
            # Note: never include headers/token in the error message.
            logger.warning("Vault request failed: %s %s", type(exc).__name__, path)
            raise ccxt.NetworkError(f"Vault unreachable for {path}: {type(exc).__name__}") from exc

        if response.status_code in (401, 403):
            logger.warning("Vault auth/lease rejection (HTTP %s) on %s", response.status_code, path)
            raise ccxt.AuthenticationError(
                f"Vault rejected request (HTTP {response.status_code}) on {path}"
            )
        if response.status_code == 429:
            raise ccxt.RateLimitExceeded(f"Vault rate limit exceeded on {path}")
        if 400 <= response.status_code < 500:
            raise ccxt.ExchangeError(f"Vault error (HTTP {response.status_code}) on {path}")
        if response.status_code >= 500:
            raise ccxt.ExchangeNotAvailable(f"Vault error (HTTP {response.status_code}) on {path}")

        try:
            return response.json()
        except ValueError as exc:
            raise ccxt.ExchangeError(f"Vault returned invalid JSON on {path}") from exc

    # ------------------------------------------------------------------
    # Vault proxy API
    # ------------------------------------------------------------------

    def acquire_lease(
        self,
        vault_key_id: str,
        bot_id: str,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    ) -> str:
        """
        Create a fresh proxy lease. POST /v1/proxy/{vault_key_id}/lease

        :return: lease_id (str) to pass via X-Vault-Lease on proxy calls.
        :raises ccxt.AuthenticationError: on any failure to obtain a lease.
        """
        body = {
            "bot_id": str(bot_id),
            "backend_instance_id": self._instance_id,
            "lease_ttl_seconds": lease_ttl_seconds,
        }
        try:
            data = self._post(f"/v1/proxy/{vault_key_id}/lease", body)
        except ccxt.AuthenticationError:
            raise
        except ccxt.BaseError as exc:
            # Without a lease no proxy operation is possible: treat any
            # lease-acquisition failure as an authentication failure.
            raise ccxt.AuthenticationError(f"Could not acquire vault lease: {exc}") from exc

        lease_id = data.get("lease_id")
        if not lease_id:
            raise ccxt.AuthenticationError("Vault lease response missing lease_id")
        return str(lease_id)

    def validate_credentials(
        self, vault_key_id: str, lease_id: str, bot_id: str, exchange: str
    ) -> dict:
        """
        Validate stored credentials. POST /v1/proxy/{vault_key_id}/validate

        :return: {"status": "valid"|"invalid", "permissions": {...}|None}
        """
        body = {"bot_id": str(bot_id), "exchange": exchange}
        return self._post(f"/v1/proxy/{vault_key_id}/validate", body, lease_id=lease_id)

    def place_order(
        self,
        vault_key_id: str,
        lease_id: str,
        bot_id: str,
        exchange: str,
        symbol: str,
        side: str,
        type: str,  # noqa: A002 - matches the vault schema field name
        amount: float,
        price: float | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        """
        Place an order. POST /v1/proxy/{vault_key_id}/orders

        :return: {"order_id", "status", "filled_amount", "avg_price"}
        """
        body: dict = {
            "bot_id": str(bot_id),
            "exchange": exchange,
            "symbol": symbol,
            "side": side,
            "type": type,
            "amount": str(amount),
        }
        if price is not None:
            body["price"] = str(price)
        if client_order_id is not None:
            body["client_order_id"] = client_order_id
        return self._post(f"/v1/proxy/{vault_key_id}/orders", body, lease_id=lease_id)

    def cancel_order(
        self, vault_key_id: str, lease_id: str, bot_id: str, exchange: str, order_id: str
    ) -> dict:
        """
        Cancel an order. POST /v1/proxy/{vault_key_id}/orders/cancel

        :return: {"status": "cancelled"|"not_found"|"rejected"}
        """
        body = {"bot_id": str(bot_id), "exchange": exchange, "order_id": str(order_id)}
        return self._post(f"/v1/proxy/{vault_key_id}/orders/cancel", body, lease_id=lease_id)

    def get_balances(self, vault_key_id: str, lease_id: str, bot_id: str, exchange: str) -> dict:
        """
        Fetch sanitized balances. POST /v1/proxy/{vault_key_id}/balances

        :return: {"balances": [{"asset", "available", "locked"}, ...]}
        """
        body = {"bot_id": str(bot_id), "exchange": exchange}
        return self._post(f"/v1/proxy/{vault_key_id}/balances", body, lease_id=lease_id)
