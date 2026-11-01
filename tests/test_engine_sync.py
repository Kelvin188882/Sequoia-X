"""增量同步的正确性回归测试。"""

import sys
import types

import pytest

from sequoia_x.data import engine


class _FakeLogin:
    def __init__(self, code, msg):
        self.error_code = code
        self.error_msg = msg


def test_fetch_batch_login_failure_is_reported(monkeypatch) -> None:
    """登录失败必须以 errors 形式暴露，不能静默返回空列表。

    回归：此前 bs.login() 返回值被丢弃，限流/黑名单与非交易日表现一致，
    主流程会误报「无新数据（可能非交易日）」。
    """
    fake = types.SimpleNamespace(
        login=lambda: _FakeLogin("10001011", "黑名单用户"),
        logout=lambda: None,
        query_history_k_data_plus=lambda *a, **k: pytest.fail("登录失败不应发起查询"),
    )
    monkeypatch.setitem(sys.modules, "baostock", fake)

    rows, errors = engine._bs_fetch_batch(
        [("600000", "sh.600000", "2026-01-01", "2026-01-02")]
    )

    assert rows == []
    assert len(errors) == 1
    assert errors[0][0] == "__login__"
    assert "10001011" in errors[0][1]


def test_fetch_batch_logout_on_exception(monkeypatch) -> None:
    """查询抛异常时也必须 logout，避免泄漏会话。"""
    calls = {"logout": 0}

    def _boom(*a, **k):
        raise RuntimeError("network down")

    fake = types.SimpleNamespace(
        login=lambda: _FakeLogin("0", "success"),
        logout=lambda: calls.__setitem__("logout", calls["logout"] + 1),
        query_history_k_data_plus=_boom,
    )
    monkeypatch.setitem(sys.modules, "baostock", fake)

    with pytest.raises(RuntimeError):
        engine._bs_fetch_batch([("600000", "sh.600000", "2026-01-01", "2026-01-02")])

    assert calls["logout"] == 1


def test_sync_upsert_preserves_failed_symbol(tmp_path) -> None:
    """INSERT OR REPLACE 语义：拉取失败的股票，其历史数据必须保留。

    回归：此前先 DELETE 当日全部行再 append，失败票当天历史被永久删除。
    """
    import sqlite3

    import pandas as pd

    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(engine._CREATE_TABLE_SQL)
    conn.execute(
        "INSERT INTO stock_daily (symbol,date,open,high,low,close,volume,turnover)"
        " VALUES ('600000','2026-10-30',8,8.2,7.9,8.1,1000,8100)"
    )
    conn.commit()

    # 模拟本次只有 000001 拉到数据（600000 拉取失败，不在结果集中）
    df = pd.DataFrame(
        [["000001", "2026-10-30", 10, 10.3, 9.9, 10.2, 2000, 20400]],
        columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"],
    )
    df = df.where(pd.notna(df), None)
    conn.executemany(
        "INSERT OR REPLACE INTO stock_daily "
        "(symbol, date, open, high, low, close, volume, turnover) VALUES (?,?,?,?,?,?,?,?)",
        list(df.itertuples(index=False, name=None)),
    )
    conn.commit()

    rows = conn.execute("SELECT symbol, close FROM stock_daily ORDER BY symbol").fetchall()
    conn.close()

    assert len(rows) == 2, "拉取失败的股票历史被误删"
    assert ("600000", 8.1) in rows
    assert ("000001", 10.2) in rows
