# Must-do checklist — nerdbot (freqtrade fork)

Complete these before putting real exchange balances behind this engine. Paper trading only until every item is done.

## Critical

- [ ] Build and run the engine image end-to-end before any live deploy
- [ ] Align with backend: bots must receive only a scoped vault lease — never the shared master vault token
- [ ] Keep the gate that refuses to start if exchange keys appear in the bot environment; enforce it in CI
- [ ] Keep using the current upstream engine image; do not rely on this fork's lag behind upstream for security

## Hardening

- [ ] Document which image tag / upstream base is used in production
- [ ] Confirm the vault adapter never logs or persists credentials
- [ ] Keep container lockdown settings (read-only FS, no caps, `no-new-privileges`, non-root) when building deploy images

## Ops

- [ ] Confirm the image was actually built and smoke-tested in the target environment (local notes said it was never built there)
- [ ] Outside security review after vault scoping lands

## Context

This fork routes trades through nerdbot-vault. It should never hold exchange API keys. Safety depends on the vault lease model and the backend not injecting the shared master password into containers.
