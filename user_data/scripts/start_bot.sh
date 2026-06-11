#!/usr/bin/env bash
#
# nerdbot entrypoint: launches Freqtrade with the nerdbot_vault exchange
# adapter. Exchange credentials NEVER reach this container - all credentialed
# operations are proxied through the nerdbot-vault service.
#
set -euo pipefail

# ---------------------------------------------------------------------------
# HARD GUARD: this container must NEVER hold raw exchange credentials.
# If any credential-looking variable is set, refuse to start.
# ---------------------------------------------------------------------------
CREDENTIAL_LEAK=0
for forbidden_var in EXCHANGE_API_KEY EXCHANGE_API_SECRET EXCHANGE_API_PASSPHRASE; do
    if [ -n "${!forbidden_var:-}" ]; then
        echo "FATAL: ${forbidden_var} is set in the environment." >&2
        echo "FATAL: raw exchange credentials must NEVER reach this container." >&2
        echo "FATAL: all credentialed operations go through the nerdbot-vault proxy." >&2
        CREDENTIAL_LEAK=1
    fi
done
if [ "${CREDENTIAL_LEAK}" -ne 0 ]; then
    exit 1
fi

# ---------------------------------------------------------------------------
# Required vault/bot configuration.
# ---------------------------------------------------------------------------
: "${VAULT_BASE_URL:?VAULT_BASE_URL is required (nerdbot-vault base URL)}"
: "${VAULT_KEY_ID:?VAULT_KEY_ID is required (vault key UUID)}"
: "${BACKEND_TOKEN:?BACKEND_TOKEN is required (backend bearer token)}"
: "${BACKEND_INSTANCE_ID:?BACKEND_INSTANCE_ID is required (backend instance id)}"
: "${REAL_EXCHANGE:?REAL_EXCHANGE is required (binance|kraken|coinbase)}"
: "${BOT_ID:?BOT_ID is required (bot UUID)}"

# ---------------------------------------------------------------------------
# Register the nerdbot_vault exchange adapter at interpreter startup:
# user_data/exchange contains sitecustomize.py, which Python auto-imports
# when the directory is on PYTHONPATH (this happens before freqtrade boots).
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTER_DIR="${NERDBOT_ADAPTER_DIR:-${SCRIPT_DIR}/../exchange}"
if [ -d "${ADAPTER_DIR}" ]; then
    ADAPTER_DIR="$(cd "${ADAPTER_DIR}" && pwd)"
fi
export PYTHONPATH="${ADAPTER_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

echo "Starting Freqtrade via nerdbot-vault proxy (exchange=${REAL_EXCHANGE}," \
     "bot_id=${BOT_ID}, paper=${IS_PAPER_TRADING:-0})"

exec freqtrade trade --config /freqtrade/config/config.json --strategy "${STRATEGY:-SampleStrategy}"
