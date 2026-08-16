"""
NerdbotStrategy - the Nerdbot platform's flagship default strategy.

A deliberately readable, conservative, long-only strategy built from three
classic indicators:

- RSI (14)                  - momentum / oversold detection
- MACD (12 / 26 / 9)        - trend confirmation
- Bollinger Bands (20, 2sd) - mean-reversion context

Entry (long): RSI recovers from oversold (crosses up through 30) while price
is still in the lower half of the Bollinger channel and MACD confirms that
momentum has already turned bullish.

Exit (long): RSI pushes into overbought (crosses up through 70) or price
breaks out above the upper Bollinger band - both classic "take the profit"
signals for a mean-reversion entry.

The strategy is fully self-contained: no network calls, no external
services, only pandas / TA-Lib / qtpylib (all shipped in the freqtrade
image). It works out of the box in dry-run with a StaticPairList.

The AI-enhanced variant (NerdbotAIStrategy, same directory) subclasses this
strategy and must never change these base signals - it may only veto entries
using the nerdbot-ai score.
"""

from pandas import DataFrame

from freqtrade.strategy import IStrategy

import talib.abstract as ta
from technical import qtpylib


class NerdbotStrategy(IStrategy):
    """Classic RSI + MACD + Bollinger long-only strategy (5m, spot)."""

    INTERFACE_VERSION = 3

    # Spot only - the Nerdbot platform never opens short positions.
    can_short: bool = False

    # Optimal timeframe for the strategy (matches the config templates).
    timeframe = "5m"

    # Conservative profit ladder: take 3% immediately if offered, scale the
    # target down as the trade ages, and get out at break-even after 3 hours
    # so stale positions don't linger.
    minimal_roi = {
        "0": 0.03,
        "30": 0.02,
        "60": 0.01,
        "180": 0.0,
    }

    # Conservative hard stop: a 5m mean-reversion entry that moves 5% against
    # us is simply wrong - cut it rather than ride it to the sample default
    # of -10%.
    stoploss = -0.05

    # Keep exits deterministic - no trailing stop in v1.
    trailing_stop = False

    # Only evaluate signals once per new candle.
    process_only_new_candles = True

    # Exit signals are part of the strategy design.
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    # Longest lookback is MACD's 26-period slow EMA (plus its 9-period
    # signal). EMAs converge asymptotically, so 100 candles gives the MACD
    # a properly settled warm-up at the window edge while staying well
    # within Kraken's no-history OHLCV limit (720 candles).
    startup_candle_count: int = 100

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    plot_config = {
        "main_plot": {
            "bb_upperband": {},
            "bb_middleband": {},
            "bb_lowerband": {},
        },
        "subplots": {
            "MACD": {
                "macd": {"color": "blue"},
                "macdsignal": {"color": "orange"},
            },
            "RSI": {
                "rsi": {"color": "red"},
            },
        },
    }

    def informative_pairs(self):
        """No additional informative pairs - the strategy is self-contained."""
        return []

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Compute RSI, MACD and Bollinger Bands."""
        # RSI (14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)

        # MACD (12 / 26 / 9)
        macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]

        # Bollinger Bands (20, 2 standard deviations) on typical price
        bollinger = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2)
        dataframe["bb_lowerband"] = bollinger["lower"]
        dataframe["bb_middleband"] = bollinger["mid"]
        dataframe["bb_upperband"] = bollinger["upper"]

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Long entry: oversold recovery in the lower Bollinger half with MACD
        already bullish.
        """
        dataframe.loc[
            (
                # Signal: RSI recovers from oversold (crosses up through 30)
                qtpylib.crossed_above(dataframe["rsi"], 30)
                # Guard: price still in the lower half of the Bollinger channel
                & (dataframe["close"] < dataframe["bb_middleband"])
                # Guard: MACD confirms bullish momentum
                & (dataframe["macd"] > dataframe["macdsignal"])
                # Guard: candle actually traded
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Long exit: overbought (RSI crosses up through 70) or a breakout above
        the upper Bollinger band.
        """
        dataframe.loc[
            (
                (
                    # Signal: RSI pushes into overbought
                    qtpylib.crossed_above(dataframe["rsi"], 70)
                    # Signal: price breaks out above the upper Bollinger band
                    | qtpylib.crossed_above(dataframe["close"], dataframe["bb_upperband"])
                )
                # Guard: candle actually traded
                & (dataframe["volume"] > 0)
            ),
            "exit_long",
        ] = 1

        return dataframe
