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

# Freqtrade also reads FREQTRADE__* env overrides before normalising the
# config - FREQTRADE__EXCHANGE__KEY / __SECRET / __PASSWORD / __UID (and
# nested __CCXT_CONFIG__ keys) would smuggle raw credentials past the
# guard above, and __NAME could swap the exchange away from the vault
# adapter entirely. The exchange section is fully controlled by the
# mounted config.json, so NO env override of it is legitimate here.
while IFS= read -r env_name; do
    case "${env_name}" in
        FREQTRADE__EXCHANGE__*)
            echo "FATAL: ${env_name} is set in the environment." >&2
            echo "FATAL: Freqtrade exchange config overrides are not permitted in this container." >&2
            echo "FATAL: the exchange section is fixed by the mounted config; credentials go through the nerdbot-vault proxy." >&2
            CREDENTIAL_LEAK=1
            ;;
    esac
done < <(compgen -e)

if [ "${CREDENTIAL_LEAK}" -ne 0 ]; then
    exit 1
fi

# ---------------------------------------------------------------------------
# Required vault/bot configuration.
#
# Required even when IS_PAPER_TRADING is set: paper mode never calls the
# vault for order placement/cancellation (orders are simulated locally),
# but still uses the vault for balance reads and startup credential
# validation - so vault configuration is required in paper mode too.
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

# Config mount path is LOCKED at /freqtrade/config.json (read-only bind by
# the backend's container manager); default strategy is the Nerdbot flagship.
exec freqtrade trade --config /freqtrade/config.json --strategy "${STRATEGY:-NerdbotStrategy}"
