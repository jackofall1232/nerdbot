<p align="center">
  <img src="https://www.dropbox.com/scl/fi/xsylgobxb5mamnw1kelt1/nerdbotbanner.png?dl=1" alt="Nerdbot mission control interface">
</p>

<h1 align="center">Nerdbot</h1>
<p align="center"><strong>Open-source crypto trading engine built on Freqtrade</strong></p>

---

## Overview

**Nerdbot** is a customized, free, and open-source crypto trading bot built on top of the
excellent [Freqtrade](https://github.com/freqtrade/freqtrade) engine.

It retains full compatibility with Freqtrade while providing a curated, opinionated
distribution focused on:

- reliability
- modular strategy development
- automation via CLI, WebUI, and Telegram
- extensibility for future integrations (web platforms, messaging, automation pipelines)

Nerdbot is written in **Python 3.11+** and designed to run on Linux, macOS, and Windows.

---

## Upstream Status

Nerdbot is powered by the Freqtrade engine and follows its development closely.

Upstream project health:

[![Freqtrade CI](https://github.com/freqtrade/freqtrade/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/freqtrade/freqtrade/actions/workflows/ci.yml)
[![DOI](https://joss.theoj.org/papers/10.21105/joss.04864/status.svg)](https://doi.org/10.21105/joss.04864)
[![Coverage Status](https://coveralls.io/repos/github/freqtrade/freqtrade/badge.svg?branch=develop&service=github)](https://coveralls.io/github/freqtrade/freqtrade?branch=develop)
[![Documentation](https://readthedocs.org/projects/freqtrade/badge/)](https://www.freqtrade.io)

For full documentation of the underlying engine, please refer to  
👉 https://www.freqtrade.io

---

## Disclaimer ⚠️

This software is provided **for educational and research purposes only**.

Trading cryptocurrencies involves significant risk.  
Do **not** trade with money you cannot afford to lose.

- Always start in **Dry-Run mode**
- Understand your strategy before deploying capital
- The authors assume **no responsibility** for trading losses

---

## Supported Exchanges

Nerdbot supports all exchanges available through Freqtrade and CCXT.

### Spot Exchanges

- Binance
- BingX
- Bitget
- Bitmart
- Bybit
- Gate.io
- HTX
- Hyperliquid (DEX)
- Kraken
- OKX / MyOKX (EEA)
- Community-tested: Bitvavo, KuCoin
- Many others via [CCXT](https://github.com/ccxt/ccxt) (not guaranteed)

### Futures Exchanges (Experimental)

- Binance
- Bitget
- Gate.io
- Hyperliquid
- OKX
- Bybit

📖 Always read the  
- [Exchange Notes](docs/exchanges.md)  
- [Leverage Documentation](docs/leverage.md)

before trading.

---

## Features

- Python **3.11+**
- Dry-run trading (no capital risk)
- Backtesting & historical simulation
- Strategy optimization via machine learning (FreqAI)
- Adaptive market modeling
- Whitelists / blacklists
- Built-in WebUI
- Telegram control & monitoring
- Profit / loss reporting in fiat
- Persistent storage (SQLite)

---

## Installation & Quick Start

Nerdbot follows the same installation process as Freqtrade.

📦 **Recommended:** Docker  
📖 **Docs:**  
- Docker Quickstart  
  https://www.freqtrade.io/en/stable/docker_quickstart/
- Native installation  
  https://www.freqtrade.io/en/stable/installation/

---

## Basic Usage

Nerdbot can be invoked using either command:

```bash
nerdbot
freqtrade
