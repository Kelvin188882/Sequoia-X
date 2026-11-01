"""Sequoia-X V2 主程序入口。

两种运行模式：
  python main.py               # 日常模式：低并发增量补数据 + 跑 7 个策略 + 生成报告
                               #   （配置了飞书 Webhook 则推送，否则写 reports/ 本地报告 + HTML 看板）
  python main.py --backfill    # 回填模式：baostock 拉全市场历史K线（首次/补数据用）
                               #   低并发约 1 小时，过高并发会被 baostock 临时拉黑 IP
"""

import argparse
import sys
from dotenv import load_dotenv
load_dotenv()

from datetime import date

import socket
socket.setdefaulttimeout(10.0)

from sequoia_x.core.config import get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.notify.report import LocalReporter
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequoia-X V2 选股系统")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="回填模式：通过 baostock 拉取全市场历史 K 线（低并发约 1 小时，可中断续传）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="回填模式的并行进程数（默认取 .env 的 BACKFILL_WORKERS，建议 2~4）",
    )
    args = parser.parse_args()

    try:
        # 1. 初始化配置
        settings = get_settings()

        # 2. 初始化日志
        logger = get_logger(__name__)
        logger.info("Sequoia-X V2 启动")

        # 3. 初始化数据引擎
        engine = DataEngine(settings)

        if args.backfill:
            # ── 回填模式：单线程保守拉历史 K 线，自动多轮重跑 ──
            logger.info("进入回填模式...")
            all_symbols = engine.get_all_symbols()
            engine.backfill(all_symbols, workers=args.workers or settings.backfill_workers)
            logger.info("Sequoia-X V2 回填模式运行完成")
            return

        # ── 日常模式：单次 API 补今天 + 策略 + 推送 ──
        logger.info("开始拉取最新快照...")
        count = engine.sync_today_bulk()
        logger.info(f"快照同步完成，写入 {count} 只股票")

        # 4. 策略列表（新增策略在此追加即可）
        strategies: list[BaseStrategy] = [
            MaVolumeStrategy(engine=engine, settings=settings),
            TurtleTradeStrategy(engine=engine, settings=settings),
            HighTightFlagStrategy(engine=engine, settings=settings),
            LimitUpShakeoutStrategy(engine=engine, settings=settings),
            UptrendLimitDownStrategy(engine=engine, settings=settings),
            RpsBreakoutStrategy(engine=engine, settings=settings),
            PrivatePlacementStrategy(engine=engine, settings=settings),
        ]

        notifier = FeishuNotifier(settings) if settings.feishu_enabled else LocalReporter(settings)
        if settings.feishu_enabled:
            logger.info("飞书 Webhook 已配置，选股结果推送飞书")
        else:
            logger.info("未配置飞书 Webhook，选股结果将写入本地报告 reports/")

        # 5. 遍历策略，有结果则推送（本地报告模式记录全部策略，含空结果）
        for strategy in strategies:
            strategy_name = type(strategy).__name__
            logger.info(f"执行策略：{strategy_name}")

            selected: list[str] = strategy.run()
            logger.info(f"{strategy_name} 选出 {len(selected)} 只股票")

            if selected or getattr(notifier, "report_empty", False):
                notifier.send(
                    symbols=selected,
                    strategy_name=strategy_name,
                    webhook_key=strategy.webhook_key,
                )
            else:
                logger.info(f"{strategy_name} 无选股结果，跳过推送")

        if hasattr(notifier, "finalize"):
            notifier.finalize()

        # 6. 生成 HTML 看板（失败不影响主流程）
        try:
            from sequoia_x.notify.html import build_html

            build_html(settings)
        except Exception as exc:
            logger.warning(f"HTML 看板生成失败（Markdown 报告不受影响）：{exc}")

    except Exception:
        try:
            _logger = get_logger(__name__)
            _logger.exception("主流程发生未捕获异常，程序终止")
        except Exception:
            import traceback
            traceback.print_exc()
        sys.exit(1)

    logger.info("Sequoia-X V2 运行完成")


if __name__ == "__main__":
    main()
