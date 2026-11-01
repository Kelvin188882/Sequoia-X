"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import sqlite3
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
"""


def _bs_fetch_batch(tasks: list) -> tuple[list, list]:
    """多进程 worker：独立 login，批量拉取 baostock 增量数据。

    Returns:
        (rows, errors)：rows 为 [symbol, date, open, ...] 数据行；
        errors 为 [(symbol, 错误描述), ...]，symbol 为 "__login__" 表示登录级失败。

        登录或查询失败不再静默跳过——否则限流/黑名单与非交易日在主流程里
        表现完全一致（都是 0 行），会生成基于旧数据的报告却毫无告警。
    """
    import baostock as bs

    rows: list = []
    errors: list = []
    lg = bs.login()
    if lg.error_code != "0":
        return rows, [("__login__", f"{lg.error_code} {lg.error_msg}")]

    try:
        for symbol, bs_code, start, end in tasks:
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,amount",
                start_date=start,
                end_date=end,
                frequency="d",
                adjustflag="1",  # 后复权
            )
            if rs.error_code != "0":
                errors.append((symbol, f"{rs.error_code} {rs.error_msg}"))
                continue
            while rs.next():
                rows.append([symbol] + rs.get_row_data())
    finally:
        bs.logout()  # 异常也必须释放会话，否则会泄漏并中断整批同步
    return rows, errors


_BLACKLIST_HINTS = ("黑名单", "10001011", "频繁", "limit")


def _bs_login_with_probe(max_retries: int = 3) -> tuple[bool, str]:
    """带重试的 baostock 登录探针，用于提前发现限流/黑名单。

    Returns:
        (是否成功, 错误描述)
    """
    import time

    import baostock as bs

    msg = ""
    for attempt in range(max_retries):
        lg = bs.login()
        if lg.error_code == "0":
            return True, ""
        msg = f"{lg.error_code} {lg.error_msg}"
        if any(h in msg for h in _BLACKLIST_HINTS):
            return False, msg  # 被限流，立即返回，避免加重封禁
        time.sleep(5 * (attempt + 1))
    return False, msg


def _bs_backfill_chunk(job: tuple) -> tuple[int, int, int]:
    """多进程 worker：回填一批股票的历史日 K（后复权），直接写 SQLite。

    Args:
        job: (tasks, db_path)，tasks 为 [(symbol, bs_code, start, end), ...]

    Returns:
        (成功数, 跳过数, 失败数)
    """
    import time

    import baostock as bs

    tasks, db_path = job
    ok = skip = fail = 0

    if not _bs_login_with_probe(max_retries=2)[0]:
        return 0, 0, len(tasks)

    def _f(value: str) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    conn = sqlite3.connect(db_path, timeout=120)
    aborted = False
    try:
        for idx, (symbol, bs_code, start, end) in enumerate(tasks):
            rows = None
            for attempt in range(3):
                try:
                    rs = bs.query_history_k_data_plus(
                        bs_code,
                        "date,open,high,low,close,volume,amount",
                        start_date=start,
                        end_date=end,
                        frequency="d",
                        adjustflag="1",  # 后复权
                    )
                    if rs.error_code != "0":
                        raise RuntimeError(rs.error_msg)
                    rows = []
                    while rs.next():
                        rows.append(rs.get_row_data())
                    break
                except Exception as exc:
                    rows = None
                    if any(h in str(exc) for h in _BLACKLIST_HINTS):
                        aborted = True
                        break
                    time.sleep(2 ** (attempt + 1))
                    bs.logout()
                    re_ok, msg = _bs_login_with_probe(max_retries=1)
                    if not re_ok:
                        if any(h in msg for h in _BLACKLIST_HINTS):
                            aborted = True
                            break
            if aborted:
                fail += len(tasks) - idx  # 剩余任务全部记为未完成
                break

            if rows is None:
                fail += 1
                continue

            payload = []
            for r in rows:
                close = _f(r[4])
                volume = _f(r[5])
                if close is None or not volume or volume <= 0:
                    continue  # 停牌 / 无效行
                open_ = _f(r[1]) or close
                high = _f(r[2]) or close
                low = _f(r[3]) or close
                payload.append((symbol, r[0], open_, high, low, close, volume, _f(r[6]) or 0.0))

            if not payload:
                skip += 1
                continue

            conn.executemany(
                "INSERT OR IGNORE INTO stock_daily "
                "(symbol, date, open, high, low, close, volume, turnover) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
            conn.commit()
            ok += 1
    finally:
        conn.close()
        bs.logout()

    return ok, skip, fail


class DataEngine:
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        self.sync_workers: int = settings.sync_workers
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")  # 支持多进程并发写入
            conn.execute("PRAGMA busy_timeout=120000")
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.commit()
        logger.info(f"数据库初始化完成：{self.db_path}")

    def _get_last_date(self, symbol: str) -> str | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM stock_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。"""
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    # ── 数据同步 ──

    def sync_today_bulk(self) -> int:
        """多进程并行通过 baostock 拉取增量数据（后复权），写入 SQLite。"""
        from datetime import date, timedelta
        from multiprocessing import Pool

        today_str = date.today().strftime("%Y-%m-%d")

        tasks = []
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall()

        if not rows:
            logger.warning("本地无股票数据，请先执行 --backfill")
            return 0

        for symbol, last_date in rows:
            if last_date and last_date >= today_str:
                continue
            start = today_str
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

        if not tasks:
            logger.info("所有股票已是最新，无需更新")
            return 0

        logger.info(f"需要更新 {len(tasks)} 只股票，启动多进程并行拉取...")

        n_workers = max(1, min(self.sync_workers, len(tasks)))
        chunks = [tasks[i::n_workers] for i in range(n_workers)]

        with Pool(n_workers) as pool:
            batch_results = pool.map(_bs_fetch_batch, chunks)

        all_rows: list = []
        all_errors: list = []
        for rows, errors in batch_results:
            all_rows.extend(rows)
            all_errors.extend(errors)

        # 错误必须显式暴露：限流/黑名单/网络故障与非交易日不能共用一句日志
        login_errors = [m for s, m in all_errors if s == "__login__"]
        query_errors = [(s, m) for s, m in all_errors if s != "__login__"]
        if login_errors:
            logger.error(
                f"baostock 登录失败（{len(login_errors)}/{n_workers} 个 worker）：{login_errors[0]}。"
                "若为黑名单/限流，本次同步无效，请降低 SYNC_WORKERS 或稍后重试。"
            )
        if query_errors:
            logger.warning(
                f"本次 {len(query_errors)} 只股票拉取失败（其历史数据已保留，不受影响），"
                f"示例：{query_errors[:3]}"
            )

        if not all_rows:
            if all_errors:
                logger.error("未获取到任何新数据且存在上述错误，请勿视为非交易日。")
            else:
                logger.info("无新数据（可能非交易日）")
            return 0

        df = pd.DataFrame(all_rows, columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"])
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0]

        count = len(df)
        # 用 INSERT OR REPLACE 而非「先 DELETE 再 append」：
        # 先删后插时，若某只股票本次拉取失败（停牌/超时/worker 出错），
        # 它当天的历史会被永久删掉。UNIQUE(symbol, date) 约束本就支持幂等覆盖。
        df = df.where(pd.notna(df), None)  # NaN -> None，便于 sqlite 绑定为 NULL
        payload = list(df.itertuples(index=False, name=None))
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO stock_daily "
                "(symbol, date, open, high, low, close, volume, turnover) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
            conn.commit()

        logger.info(f"sync_today_bulk: 写入 {count} 条数据")
        return count

    def backfill(self, symbols: list[str], workers: int = 2) -> None:
        """多进程并行回填历史日 K 线（后复权）。

        容错与续传：
        - 已入库到最新交易日的股票自动 skip，中断后重跑可续传
        - 每个 worker 独立 login baostock，单只失败重试 3 次并重连
        - 采用 INSERT OR IGNORE，重复写入自动忽略

        Args:
            symbols: 待回填的股票代码列表（纯数字）。
            workers: 并行进程数，默认 2（过高会被 baostock 判为滥用并拉黑 IP）。
        """
        from datetime import date, timedelta
        from multiprocessing import Pool

        today_str = date.today().strftime("%Y-%m-%d")

        tasks = []
        skipped = 0
        for symbol in symbols:
            last_date = self._get_last_date(symbol)
            if last_date and last_date >= today_str:
                skipped += 1
                continue
            start = self.start_date
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

        if not tasks:
            logger.info(f"所有股票数据已是最新，无需回填（跳过 {skipped} 只）")
            return

        n_workers = max(1, min(workers, len(tasks)))
        chunks = [tasks[i::n_workers] for i in range(n_workers)]
        logger.info(
            f"需要回填 {len(tasks)} 只股票（已最新跳过 {skipped} 只），"
            f"启动 {n_workers} 进程并行拉取..."
        )

        # 先探测登录：baostock 对高频/高并发访问会临时拉黑 IP
        login_ok, login_msg = _bs_login_with_probe(max_retries=2)
        if not login_ok:
            logger.error(
                f"baostock 登录被拒绝（{login_msg}）。"
                "通常是并发过高触发了限流/黑名单，请等待一段时间后降低 --workers 重试。"
            )
            return
        import baostock as bs
        bs.logout()

        with Pool(n_workers) as pool:
            results = pool.map(_bs_backfill_chunk, [(c, self.db_path) for c in chunks])

        ok = sum(r[0] for r in results)
        skip = skipped + sum(r[1] for r in results)
        fail = sum(r[2] for r in results)
        logger.info(f"回填完成 — 成功: {ok} | 跳过: {skip} | 失败: {fail}")

    # ── 股票列表 ──

    def get_all_symbols(self) -> list[str]:
        """通过 baostock 获取全市场 A 股代码列表。"""
        import baostock as bs

        lg = bs.login()
        if lg.error_code != "0":
            logger.error(f"baostock 登录失败: {lg.error_msg}")
            return []

        try:
            rs = bs.query_stock_basic(code_name="", code="")
            symbols = []
            names: dict[str, str] = {}
            while rs.next():
                row = rs.get_row_data()
                code = row[0]           # "sh.600000" or "sz.000001"
                status = row[4]         # "1" = 上市
                stock_type = row[5]     # "1" = 股票
                if status == "1" and stock_type == "1":
                    plain = code.split(".")[1]  # 提取纯数字代码
                    symbols.append(plain)
                    names[plain] = row[1]       # 股票名称
            logger.info(f"获取股票列表完成，共 {len(symbols)} 只")
            self._save_names(names)
            return symbols
        except Exception as e:
            logger.error(f"获取股票列表失败: {e}")
            return []
        finally:
            bs.logout()

    def _save_names(self, names: dict[str, str]) -> None:
        """缓存股票代码→名称映射，供本地报告渲染使用。"""
        import json

        cache = Path(self.db_path).parent / "stock_names.json"
        try:
            cache.write_text(json.dumps(names, ensure_ascii=False, indent=0), encoding="utf-8")
            logger.info(f"股票名称缓存已更新：{cache}")
        except Exception as exc:
            logger.warning(f"股票名称缓存写入失败：{exc}")

    def get_local_symbols(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM stock_daily"
            ).fetchall()
        return [row[0] for row in rows]
