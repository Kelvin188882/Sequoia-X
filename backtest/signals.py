"""策略信号的向量化实现（用于历史回测）。

每个函数接收【单只股票】按日期升序的 DataFrame，返回布尔 Series（索引对齐），
True 表示该交易日收盘后触发买入信号。

阈值与 sequoia_x/strategy/ 下的实盘策略严格一致，只是把「取最后两行判断」
改写为「对全部历史逐日判断」，且全部使用 shift/rolling，不含未来函数。

RPS 是横截面策略，需要全市场排名，这里只产出每股的分项（涨幅、滚动高点），
排名在 engine 中按日期横截面合成。
"""

from __future__ import annotations

import pandas as pd

STRATEGIES = ["ma_volume", "turtle", "flag", "shakeout", "limit_down", "rps"]


def sig_ma_volume(df: pd.DataFrame) -> pd.Series:
    """5 日均线上穿 20 日均线（金叉）+ 当日量 > 20 日均量 1.5 倍。"""
    close, vol = df["close"], df["volume"]
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    vol_ma20 = vol.rolling(20).mean()
    golden_cross = (ma5 > ma20) & (ma5.shift(1) < ma20.shift(1))
    surge = vol > vol_ma20 * 1.5
    return (golden_cross & surge).fillna(False)


def sig_turtle(df: pd.DataFrame) -> pd.Series:
    """收盘突破前 20 日最高 + 成交额过亿 + 实体阳线且真涨。"""
    o, h, c, t = df["open"], df["high"], df["close"], df["turnover"]
    high20 = h.shift(1).rolling(20).max()
    breakout = c > high20
    liquid = t > 100_000_000
    is_yang = c > o
    is_up = c > c.shift(1)
    return (breakout & liquid & is_yang & is_up).fillna(False)


def sig_flag(df: pd.DataFrame) -> pd.Series:
    """40 日涨幅>60% + 近 10 日振幅<15% + 高位抗跌 + 缩量至均量 60% 以下。"""
    h, l, v = df["high"], df["low"], df["volume"]
    high40, low40 = h.rolling(40).max(), l.rolling(40).min()
    high10, low10 = h.rolling(10).max(), l.rolling(10).min()
    momentum = high40 / low40 > 1.6
    consolidation = high10 / low10 < 1.15
    high_level = low10 >= high40 * 0.8
    vol_ma20 = v.shift(1).rolling(20).mean()  # 前 20 日均量（不含当日）
    shrink = v < vol_ma20 * 0.6
    return (momentum & consolidation & high_level & shrink).fillna(False)


def sig_shakeout(df: pd.DataFrame) -> pd.Series:
    """昨日涨停 + 今日收阴 + 今日量>昨日 2 倍 + 今日最低≥昨收。"""
    o, c, l, v = df["open"], df["close"], df["low"], df["volume"]
    limit_up_yesterday = c.shift(1) >= c.shift(2) * 1.095
    bearish = c < o
    surge = v > v.shift(1) * 2.0
    support = l >= c.shift(1)
    return (limit_up_yesterday & bearish & surge & support).fillna(False)


def sig_limit_down(df: pd.DataFrame) -> pd.Series:
    """昨日 20 日线>60 日线（上升趋势）+ 今日放量跌停。"""
    c, v = df["close"], df["volume"]
    ma20 = c.rolling(20).mean()
    ma60 = c.rolling(60).mean()
    vol_ma20 = v.rolling(20).mean()
    uptrend = ma20.shift(1) > ma60.shift(1)
    limit_down = c <= c.shift(1) * 0.905
    surge = v > vol_ma20 * 2.0
    return (uptrend & limit_down & surge).fillna(False)


def rps_components(df: pd.DataFrame, period: int = 120) -> tuple[pd.Series, pd.Series]:
    """RPS 每股分项：period 日涨幅、period 日滚动最高价。"""
    c, h = df["close"], df["high"]
    pct = (c - c.shift(period)) / c.shift(period)
    roll_high = h.rolling(period, min_periods=period // 2).max()
    return pct, roll_high


# 非横截面策略 → 信号函数（RPS 单独处理）
SIGNAL_FUNCS = {
    "ma_volume": sig_ma_volume,
    "turtle": sig_turtle,
    "flag": sig_flag,
    "shakeout": sig_shakeout,
    "limit_down": sig_limit_down,
}
