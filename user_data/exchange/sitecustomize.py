"""
Interpreter-startup hook that registers the nerdbot_vault exchange.

``start_bot.sh`` prepends this directory to PYTHONPATH; Python's ``site``
module then imports this file automatically at interpreter startup - BEFORE
``freqtrade trade`` runs. Importing ``nerdbot_vault`` registers:

- the 'nerdbot_vault' ccxt shim (so config validation and ccxt init accept
  the exchange name), and
- the ``Nerdbot_Vault`` class in the ``freqtrade.exchange`` namespace (so
  ``ExchangeResolver`` resolves ``"exchange": {"name": "nerdbot_vault"}``).

This keeps Freqtrade core completely unmodified.
"""

import sys


try:
    import nerdbot_vault  # noqa: F401 - import performs the registration
except Exception as exc:
    print(
        f"sitecustomize: nerdbot_vault registration failed: {type(exc).__name__}: {exc}",
        file=sys.stderr,
    )
