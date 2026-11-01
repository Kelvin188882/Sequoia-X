"""轻量历史回测引擎：本地 SQLite 数据 + 向量化信号 + 多档持有期收益。

回测口径（避免未来函数）：
  - 信号日 T：用 ≤T 的数据判定出信号（与实盘一致）
  - 买入：T+1 开盘价
  - 卖出：T+N 收盘价（N 为持有交易日数，N=1 即 T+1 当天开盘买收盘卖）
  - 净收益 = close[T+N] / open[T+1] - 1 - 往返成本

卖出规则采用「固定持有期」，因为策略本身只给买点不给卖点；
多档持有期对比能看出信号的衰减曲线，且无过拟合。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from backtest.signals import SIGNAL_FUNCS, STRATEGIES, rps_components

HOLD_PERIODS = [1, 3, 5, 10, 20]


@dataclass
class BacktestResult:
    """单策略单持有期的统计结果。"""

    strategy: str
    hold: int
    count: int = 0
    win_rate: float = np.nan
    avg: float = np.nan
    median: float = np.nan
    total: float = np.nan
    profit_factor: float = np.nan
    avg_win: float = np.nan
    avg_loss: float = np.nan
    best: float = np.nan
    worst: float = np.nan


def load_data(db_path: str) -> pd.DataFrame:
    """加载全市场日线，按 symbol/date 排序。"""
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql(
            "SELECT symbol, date, open, high, low, close, volume, turnover FROM stock_daily",
            conn,
        )
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["symbol", "date"]).reset_index(drop=True)


def _per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    """对单只股票计算全部非横截面信号 + RPS 分项 + 各持有期收益。"""
    out = df.copy()
    # 新版 pandas 的 groupby.apply 会排除 group 键列，这里用 group 名补回 symbol
    if "symbol" not in out.columns:
        out["symbol"] = getattr(df, "name", None)
    for name, func in SIGNAL_FUNCS.items():
        out[name] = func(df)
    pct, roll_high = rps_components(df)
    out["_rps_pct"] = pct
    out["_rps_high"] = roll_high
    out["_buy"] = df["open"].shift(-1)  # T+1 开盘价
    for n in HOLD_PERIODS:
        out[f"_ret{n}"] = df["close"].shift(-n) / df["open"].shift(-1) - 1
    return out


def build_signals(df: pd.DataFrame) -> pd.DataFrame:
    """全市场计算信号（含 RPS 横截面排名）。"""
    df = df.groupby("symbol", group_keys=False).apply(_per_symbol)

    # RPS：按日期对 period 日涨幅做横截面百分位排名
    df["_rps_rank"] = df.groupby("date")["_rps_pct"].rank(pct=True) * 100
    df["rps"] = ((df["_rps_rank"] >= 90) & (df["close"] >= df["_rps_high"] * 0.90)).fillna(False)
    return df


def _summarize(strategy: str, hold: int, rets: pd.Series) -> BacktestResult:
    r = rets.dropna()
    if len(r) == 0:
        return BacktestResult(strategy=strategy, hold=hold, count=0)
    wins = r[r > 0]
    losses = r[r <= 0]
    gross_win = wins.sum()
    gross_loss = abs(losses.sum())
    return BacktestResult(
        strategy=strategy,
        hold=hold,
        count=len(r),
        win_rate=len(wins) / len(r) * 100,
        avg=r.mean() * 100,
        median=r.median() * 100,
        total=r.sum() * 100,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else np.inf,
        avg_win=(wins.mean() * 100) if len(wins) else 0.0,
        avg_loss=(losses.mean() * 100) if len(losses) else 0.0,
        best=r.max() * 100,
        worst=r.min() * 100,
    )


def run(
    df: pd.DataFrame,
    start: str,
    end: str,
    cost: float = 0.001,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """执行回测。

    Returns:
        (summary, trades)：summary 为策略×持有期统计表；trades 为全部信号明细。
    """
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    in_range = (df["date"] >= start_ts) & (df["date"] <= end_ts) & df["_buy"].notna()

    summaries: list[BacktestResult] = []
    trade_frames: list[pd.DataFrame] = []

    for strat in STRATEGIES:
        sig_mask = in_range & df[strat]
        for n in HOLD_PERIODS:
            ret_col = f"_ret{n}"
            valid = sig_mask & df[ret_col].notna()
            rets = df.loc[valid, ret_col] - cost
            summaries.append(_summarize(strat, n, rets))

            if n == HOLD_PERIODS[0]:  # 明细只导一次（含所有持有期收益）
                pass
        # 导出该策略全部信号明细（含各持有期毛收益）
        cols = ["symbol", "date", "_buy"] + [f"_ret{n}" for n in HOLD_PERIODS]
        t = df.loc[sig_mask, cols].copy()
        t["strategy"] = strat
        trade_frames.append(t)

    summary = pd.DataFrame([s.__dict__ for s in summaries])
    trades = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    return summary, trades
