"""
nerdbot vault exchange adapter package.

Routes all credentialed exchange operations through the nerdbot-vault
credential proxy so that exchange API keys never reach this container.

Modules:
- vault_http_client: sync httpx client matching the vault proxy API contract
- market_data_client: zero-credential ccxt client for public market data
- paper_trading: in-memory order simulator for paper mode (paper mode never
  calls the vault for order placement/cancellation, but still uses the vault
  for balance reads and startup credential validation - vault configuration
  is required in paper mode too)
- nerdbot_vault: the adapter + Freqtrade drop-in Exchange subclass
- sitecustomize: auto-registration hook (via PYTHONPATH from start_bot.sh)
"""
