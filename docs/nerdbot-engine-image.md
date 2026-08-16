# Nerdbot engine image (`Dockerfile.nerdbot`)

The Nerdbot platform runs bots from a custom engine image that layers the
Nerdbot `user_data/` overlay on top of the stock freqtrade image. The stock
`Dockerfile`, `.dockerignore`, and `docker-compose.yml` are untouched.

## Build

From the repo root (requires BuildKit, the default builder since Docker 23):

```bash
docker build -f Dockerfile.nerdbot -t nerdbot-engine:latest .
```

> **Not executed in the development sandbox.** The sandbox this image was
> authored in blocks container-registry egress (Docker Hub), so the build
> above has **not** been run there. Run it in an environment with registry
> access before relying on the image; the backend's `FREQTRADE_IMAGE`
> setting defaults to `nerdbot-engine:latest`.

Because the stock `.dockerignore` excludes `user_data/`, the build uses the
sibling `Dockerfile.nerdbot.dockerignore`. BuildKit automatically prefers a
`<Dockerfile-name>.dockerignore` file for `-f Dockerfile.nerdbot` builds, so
`user_data/` is re-included for this image without modifying the stock
ignore file. (With the legacy non-BuildKit builder the per-Dockerfile ignore
file is not honored and the `COPY user_data/` step would fail - use
BuildKit.)

## What the image contains

- `freqtradeorg/freqtrade:stable` - the unmodified upstream engine
  (freqtrade core, TA-Lib, ccxt), running as the non-root user `ftuser`.
- `/freqtrade/user_data/exchange/` - the `nerdbot_vault` exchange adapter:
  credentialed operations proxy through nerdbot-vault, market data comes
  from the real exchange's public endpoints, and `sitecustomize.py`
  registers the adapter at interpreter startup.
- `/freqtrade/user_data/strategies/` - `NerdbotStrategy` (flagship RSI +
  MACD + Bollinger default) and `NerdbotAIStrategy` (the same strategy with
  a nerdbot-ai score gate on long entries).
- `/freqtrade/user_data/scripts/start_bot.sh` - the image entrypoint:
  refuses to start if any raw exchange credential is present in the
  environment, then launches
  `freqtrade trade --config /freqtrade/config.json --strategy ${STRATEGY:-NerdbotStrategy}`.
- `/freqtrade/user_data/config_templates/` - reference configs per
  exchange/mode (the backend generates the real `/freqtrade/config.json`).

## Runtime contract

| Item | Value |
|------|-------|
| Config | `/freqtrade/config.json`, read-only bind mount by the backend (locked path) |
| Entrypoint | `/freqtrade/user_data/scripts/start_bot.sh` |
| User | **Must run as uid 1000 (`ftuser`)** — freqtrade is pip-installed with `--user` into `/home/ftuser/.local`, so any other uid loses the interpreter environment; the image also bakes ftuser ownership into the writable paths below. The backend launches bot containers as uid 1000 accordingly. |
| Writable paths | `/freqtrade/data` (named volume for the sqlite db; empty volumes inherit the image path's ftuser ownership) and `/freqtrade/user_data/` — the image pre-creates `user_data/plot/` and `user_data/hyperopt_results/` because `freqtrade trade` mkdirs missing user_data subdirs at startup, which would OSError under the backend's read-only root filesystem |
| Required env | `VAULT_BASE_URL`, `VAULT_KEY_ID`, `BACKEND_TOKEN`, `BACKEND_INSTANCE_ID`, `REAL_EXCHANGE`, `BOT_ID` |
| Optional env | `IS_PAPER_TRADING`, `STRATEGY` (default `NerdbotStrategy`), `AI_SERVICE_URL`, `AI_SERVICE_TOKEN`, `IS_PRO_USER` |
| Forbidden env | `EXCHANGE_API_*`, `FREQTRADE__EXCHANGE__*` - the entrypoint exits fatally if set |

Exchange API credentials never reach this container: order placement,
cancellation, order state, and balances are proxied through nerdbot-vault
with a fresh single-use lease per call.
