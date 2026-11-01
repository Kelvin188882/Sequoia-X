"""本地报告模块：未配置飞书 Webhook 时，把选股结果写入本地 Markdown / JSON 报告。

输出位置（由 Settings.report_dir 控制，默认 reports/）：
  reports/YYYY-MM-DD.md   当日完整报告（按策略分节）
  reports/YYYY-MM-DD.json 当日结构化结果（便于程序二次处理）
  reports/latest.md       最新一次运行的报告副本
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.notify.codes import to_xueqiu_code

logger = get_logger(__name__)

STRATEGY_LABELS: dict[str, str] = {
    "ma_volume": "均线放量突破",
    "turtle": "海龟突破",
    "flag": "高窄旗形",
    "shakeout": "涨停洗盘回踩",
    "limit_down": "上升趋势跌停反包",
    "rps": "RPS 相对强度突破",
    "private_placement": "定增公告监控",
    "default": "默认",
}


class LocalReporter:
    """本地报告写入器，接口与 FeishuNotifier 对齐（send/finalize）。"""

    # 空结果也记录，方便确认每个策略当天都执行过
    report_empty: bool = True

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.report_dir = Path(settings.report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self._names: dict[str, str] | None = None
        self._results: dict[str, list[str]] = {}
        self._started_at = datetime.now()
        self._data_date = self._query_data_date()

    # ── 数据辅助 ──

    def _query_data_date(self) -> str:
        """从 SQLite 读取库内最新交易日，失败则回退到当天。"""
        try:
            with sqlite3.connect(self.settings.db_path) as conn:
                row = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()
            if row and row[0]:
                return str(row[0])
        except Exception:
            pass
        return datetime.now().strftime("%Y-%m-%d")

    def _load_names(self) -> dict[str, str]:
        """加载股票名称映射（回填时由数据引擎写入 data/stock_names.json）。"""
        if self._names is not None:
            return self._names

        names: dict[str, str] = {}
        cache = Path(self.settings.db_path).parent / "stock_names.json"
        if cache.exists():
            try:
                names = json.loads(cache.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(f"股票名称缓存读取失败：{exc}")
        self._names = names
        return names

    # ── 渲染 ──

    def _render_markdown(self) -> str:
        data_date = self._data_date
        names = self._load_names()
        total = sum(len(v) for v in self._results.values())

        lines = [
            "# Sequoia-X 选股报告",
            "",
            f"- 运行时间：{self._started_at.strftime('%Y-%m-%d %H:%M')}",
            f"- 数据截止：{data_date}",
            f"- 选股策略：{len(self._results)} 个 / 命中标的：{total} 只",
            "",
            "## 策略概览",
            "",
            "| 策略 | 命中数量 | 状态 |",
            "| --- | --- | --- |",
        ]
        for key, symbols in self._results.items():
            label = STRATEGY_LABELS.get(key, key)
            status = "有信号" if symbols else "无信号"
            lines.append(f"| {label}（{key}） | {len(symbols)} | {status} |")

        for key, symbols in self._results.items():
            label = STRATEGY_LABELS.get(key, key)
            lines += ["", f"## {label}（{key}）", ""]
            if not symbols:
                lines.append("_当日无符合条件标的_")
                continue
            lines += ["| 代码 | 名称 | 行情链接 |", "| --- | --- | --- |"]
            for code in symbols:
                name = names.get(code, code)
                lines.append(
                    f"| {code} | {name} | https://xueqiu.com/S/{to_xueqiu_code(code)} |"
                )

        lines += ["", "---", "", "_本报告由 Sequoia-X 自动生成，仅供研究参考，不构成投资建议。_", ""]
        return "\n".join(lines)

    def _persist(self) -> None:
        md = self._render_markdown()
        stamp = self._started_at.strftime("%Y-%m-%d")

        dated_md = self.report_dir / f"{stamp}.md"
        dated_md.write_text(md, encoding="utf-8")
        (self.report_dir / "latest.md").write_text(md, encoding="utf-8")

        payload = {
            "run_at": self._started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "data_date": self._data_date,
            "strategies": self._results,
            "total": sum(len(v) for v in self._results.values()),
        }
        (self.report_dir / f"{stamp}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ── 对外接口 ──

    def send(self, symbols: list[str], strategy_name: str, webhook_key: str = "default") -> None:
        """记录单个策略的选股结果并刷新本地报告。"""
        self._results[webhook_key.lower()] = list(symbols)
        self._persist()
        logger.info(
            f"本地报告已更新 [{webhook_key}] {len(symbols)} 只 → {self.report_dir}/latest.md"
        )

    def finalize(self) -> None:
        """运行结束时收尾，保证报告文件存在。"""
        self._persist()
