"""历史回测命令行入口（独立于看板/界面）。

用法：
    # 全市场，去年一年
    python -m backtest.run

    # 自定义区间 / 成本 / 小样本冒烟
    python -m backtest.run --start 2025-11-01 --end 2026-10-30 --cost 0.001
    python -m backtest.run --symbols 600000,000001,300750   # 冒烟测试

输出（backtest/results/）：
    summary_*.csv   策略 × 持有期统计
    trades_*.csv    全部信号明细（含各持有期收益）
    report_*.md     可读报告
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd

from backtest.engine import HOLD_PERIODS, build_signals, load_data, run
from backtest.signals import STRATEGIES

LABELS = {
    "ma_volume": "均线放量突破",
    "turtle": "海龟突破",
    "flag": "高窄旗形",
    "shakeout": "涨停洗盘回踩",
    "limit_down": "上升趋势跌停反包",
    "rps": "RPS 相对强度",
}


def _fmt_pct(x: float) -> str:
    return "—" if pd.isna(x) else f"{x:+.2f}%"


def render_report(summary: pd.DataFrame, start: str, end: str, cost: float, elapsed: float) -> str:
    lines = [
        "# Sequoia-X 策略历史回测报告",
        "",
        f"- 回测区间：{start} ~ {end}",
        f"- 买入：信号日 T+1 开盘价；卖出：T+N 收盘价（固定持有期）",
        f"- 往返成本：{cost*100:.2f}%（已扣）",
        f"- 计算耗时：{elapsed:.1f}s",
        "",
        "> 固定持有期回测用于衡量「信号质量」与「收益衰减」，非组合净值模拟。",
        "",
    ]
    for strat in STRATEGIES:
        sub = summary[summary["strategy"] == strat]
        lines.append(f"## {LABELS.get(strat, strat)}（{strat}）")
        lines.append("")
        lines.append("| 持有期 | 信号数 | 胜率 | 平均收益 | 中位数 | 盈亏比 | 等权累计 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for _, r in sub.iterrows():
            pf = "∞" if r["profit_factor"] == float("inf") else (f"{r['profit_factor']:.2f}" if pd.notna(r["profit_factor"]) else "—")
            wr = "—" if pd.isna(r["win_rate"]) else f"{r['win_rate']:.1f}%"
            lines.append(
                f"| {r['hold']} 日 | {r['count']} | {wr} | "
                f"{_fmt_pct(r['avg'])} | {_fmt_pct(r['median'])} | {pf} | {_fmt_pct(r['total'])} |"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Sequoia-X 策略历史回测")
    ap.add_argument("--db", default="data/sequoia_v2.db")
    ap.add_argument("--start", default="2025-11-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--cost", type=float, default=0.001, help="往返交易成本（小数，默认千一）")
    ap.add_argument("--symbols", default="", help="逗号分隔股票代码，用于小样本冒烟")
    ap.add_argument("--out", default="backtest/results")
    args = ap.parse_args()

    t0 = time.time()
    print(f"[1/4] 加载数据 {args.db} ...")
    df = load_data(args.db)
    if args.symbols:
        keep = [s.strip() for s in args.symbols.split(",") if s.strip()]
        df = df[df["symbol"].isin(keep)].copy()
        print(f"      冒烟模式：仅 {len(keep)} 只股票")
    print(f"      {df['symbol'].nunique()} 只 / {len(df)} 行，{time.time()-t0:.1f}s")

    print("[2/4] 计算全历史信号（向量化）...")
    t1 = time.time()
    df = build_signals(df)
    print(f"      完成，{time.time()-t1:.1f}s")

    print(f"[3/4] 回测 {args.start} ~ {args.end}，持有期 {HOLD_PERIODS} ...")
    summary, trades = run(df, args.start, args.end, args.cost)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = f"{args.start}_{args.end}"
    summary.to_csv(out / f"summary_{stamp}.csv", index=False)
    trades.to_csv(out / f"trades_{stamp}.csv", index=False)

    elapsed = time.time() - t0
    report = render_report(summary, args.start, args.end, args.cost, elapsed)
    (out / f"report_{stamp}.md").write_text(report, encoding="utf-8")

    print(f"[4/4] 完成，总耗时 {elapsed:.1f}s\n")
    # 控制台打印：每策略最优持有期速览
    show = summary.copy()
    show["win_rate"] = show["win_rate"].round(1)
    show["avg"] = show["avg"].round(2)
    show["total"] = show["total"].round(1)
    for strat in STRATEGIES:
        print(f"=== {LABELS.get(strat, strat)} ===")
        sub = show[show["strategy"] == strat][["hold", "count", "win_rate", "avg", "total"]]
        print(sub.to_string(index=False))
        print()
    print(f"报告：{out}/report_{stamp}.md")


if __name__ == "__main__":
    main()
