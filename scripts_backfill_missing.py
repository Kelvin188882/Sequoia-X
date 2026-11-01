"""仅回填缺失股票的历史 K 线（低并发续传）。

与 main.py --backfill 的区别：只对尚未入库的股票发起请求，
避免对已完整的股票再做一次冗余增量查询，降低被 baostock 限流的风险。

用法：
  .venv/bin/python scripts_backfill_missing.py                 # 自动对比全市场，补齐缺口
  .venv/bin/python scripts_backfill_missing.py --input list.txt # 指定代码列表（每行一个）
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import socket

socket.setdefaulttimeout(10.0)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sequoia_x.core.config import get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine

LOG = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="回填缺失股票的历史日 K")
    parser.add_argument("--workers", type=int, default=2, help="并发进程数（建议 2~4）")
    parser.add_argument("--input", default=None, help="代码列表文件（每行一个），不传则自动计算缺口")
    args = parser.parse_args()

    settings = get_settings()
    engine = DataEngine(settings)

    if args.input:
        symbols = [s.strip() for s in Path(args.input).read_text().splitlines() if s.strip()]
    else:
        all_symbols = engine.get_all_symbols()
        local = set(engine.get_local_symbols())
        symbols = [s for s in all_symbols if s not in local]

    if not symbols:
        LOG.info("无缺失股票，数据已完整")
        return

    LOG.info(f"待回填股票 {len(symbols)} 只，并发 {args.workers}")
    engine.backfill(symbols, workers=args.workers)


if __name__ == "__main__":
    main()
