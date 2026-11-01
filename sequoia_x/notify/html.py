"""HTML 呈现页：把每日选股结果渲染成单文件看板（内联 CSS，零外部依赖）。

设计令牌来自 ui-style-library：
  - 骨架：stripe（C 族 · 金融信任）——克制色 + tabular 数字 + 数字右对齐
  - 辅助：wired/theverge（A 族 · 编辑型）——强字阶对比 + 发丝线 + 无阴影
  - 字阶：1.200 小全音，33 / 23 / 16 / 12 四档（一屏 ≤4 档）
  - 颜色：纸张暖白 canvas + 红涨绿跌（A 股约定，刻意覆盖"红色只给危险"的默认规则）

用法：
    python -m sequoia_x.notify.html [--json reports/2026-11-01.json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings, get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.notify.codes import to_xueqiu_code
from sequoia_x.notify.meta import STRATEGY_META

logger = get_logger(__name__)


# ── 行情指标聚合 ──


def _load_metrics(db_path: str, symbols: list[str], lookback: int = 26) -> dict[str, dict]:
    """从 SQLite 取最近 K 线，计算涨跌幅 / 20 日涨幅 / 量比 / 成交额。

    Args:
        db_path: SQLite 路径。
        symbols: 需要计算的股票代码。
        lookback: 每只股票回看的 K 线根数（算量比需 6 根，算 20 日涨幅需 21 根）。

    Returns:
        {symbol: {"chg": 涨跌幅%, "chg20": 20日涨幅%, "amount": 成交额(亿), "vr": 量比}}
    """
    if not symbols:
        return {}

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()
        latest = row[0] if row and row[0] else date.today().strftime("%Y-%m-%d")
        cutoff = (date.fromisoformat(latest) - timedelta(days=lookback * 3)).strftime("%Y-%m-%d")

        placeholders = ",".join("?" * len(symbols))
        df = pd.read_sql(
            f"""SELECT symbol, date, close, volume, turnover
                FROM stock_daily
                WHERE symbol IN ({placeholders}) AND date >= ?
                ORDER BY symbol, date""",
            conn,
            params=[*symbols, cutoff],
        )

    if df.empty:
        return {}

    metrics: dict[str, dict] = {}
    for symbol, grp in df.groupby("symbol", sort=False):
        grp = grp.tail(lookback)
        closes = grp["close"].tolist()
        volumes = grp["volume"].tolist()
        amounts = grp["turnover"].tolist()
        dates = grp["date"].tolist()
        if len(closes) < 2:
            continue

        last_close = closes[-1]
        prev_close = closes[-2]
        chg = (last_close / prev_close - 1) * 100 if prev_close else 0.0

        # 20 日涨幅：需要 21 根 K 线，且基准日不能太旧（新股/长期停牌会让涨幅失真）
        chg20 = None
        if len(closes) >= 21 and closes[-21]:
            try:
                span = (
                    date.fromisoformat(str(dates[-1])) - date.fromisoformat(str(dates[-21]))
                ).days
            except ValueError:
                span = 0
            if 0 < span <= 45:
                chg20 = (last_close / closes[-21] - 1) * 100

        # 量比 = 当日成交量 / 前 5 日均量
        vr = None
        if len(volumes) >= 6:
            base = sum(volumes[-6:-1]) / 5
            if base:
                vr = volumes[-1] / base

        metrics[symbol] = {
            "chg": chg,
            "chg20": chg20,
            "amount": (amounts[-1] or 0) / 1e8,  # 元 → 亿元
            "vr": vr,
        }
    return metrics


# ── 渲染 ──


def _fmt_pct(value: float | None) -> tuple[str, str]:
    """格式化百分比，返回 (文本, 方向 class)。"""
    if value is None:
        return "—", "flat"
    sign = "+" if value > 0 else ""
    cls = "up" if value > 0.005 else ("down" if value < -0.005 else "flat")
    return f"{sign}{value:.2f}%", cls


def _fmt_amount(value: float) -> str:
    """成交额格式化（亿元），统一 2 位小数保证列宽一致。"""
    return f"{value:.2f}"


def _render_rows(symbols: list[str], metrics: dict[str, dict], names: dict[str, str]) -> str:
    """渲染一个策略的表格行，按涨跌幅降序。"""
    ordered = sorted(
        symbols,
        key=lambda s: (metrics.get(s, {}).get("chg") or -999),
        reverse=True,
    )
    rows: list[str] = []
    for code in ordered:
        m = metrics.get(code, {})
        name = names.get(code, code)
        chg_txt, chg_cls = _fmt_pct(m.get("chg"))
        chg20_txt, chg20_cls = _fmt_pct(m.get("chg20"))
        amount = _fmt_amount(m["amount"]) if m.get("amount") is not None else "—"
        vr = f"{m['vr']:.2f}" if m.get("vr") is not None else "—"
        xq = to_xueqiu_code(code)
        rows.append(
            f'<tr data-code="{escape(code)}" data-name="{escape(name)}">'
            f'<td class="col-code">{escape(code)}</td>'
            f'<td class="col-name"><a href="https://xueqiu.com/S/{xq}" target="_blank" '
            f'rel="noopener">{escape(name)}</a></td>'
            f'<td class="num {chg_cls}" data-l="涨跌幅">{chg_txt}</td>'
            f'<td class="num {chg20_cls}" data-l="20日涨幅">{chg20_txt}</td>'
            f'<td class="num" data-l="成交额">{amount} 亿</td>'
            f'<td class="num" data-l="量比">{vr}</td>'
            f"</tr>"
        )
    return "\n".join(rows)


def _render_strategy_section(
    key: str,
    symbols: list[str],
    metrics: dict[str, dict],
    names: dict[str, str],
) -> str:
    label, note, cond, usage = STRATEGY_META.get(key, (key, "", "", []))
    count = len(symbols)
    usage_html = ""
    if usage:
        items = "".join(f"<li>{escape(u)}</li>" for u in usage)
        usage_html = (
            '<div class="sec-usage">'
            '<span class="usage-tag">怎么用</span>'
            f"<ul>{items}</ul>"
            "</div>"
        )
    head = (
        f'<section class="strategy" id="s-{key}" data-strategy="{key}">'
        f'<div class="sec-head">'
        f'<h2>{escape(label)}</h2>'
        f'<span class="sec-count"><b>{count}</b> 只</span>'
        f"</div>"
        f'<p class="sec-note">{escape(note)}</p>'
        f'<p class="sec-cond">{escape(cond)}'
        f'{"　·　按涨跌幅降序" if symbols else ""}</p>'
        f"{usage_html}"
    )
    if not symbols:
        return head + '<p class="empty">当日无符合条件的标的</p></section>'

    table = (
        '<table class="picks">'
        "<thead><tr>"
        '<th class="col-code">代码</th><th class="col-name">名称</th>'
        '<th class="num">涨跌幅</th><th class="num">20日涨幅</th>'
        '<th class="num">成交额</th><th class="num">量比</th>'
        "</tr></thead><tbody>"
        + _render_rows(symbols, metrics, names)
        + "</tbody></table>"
    )
    return head + table + "</section>"


def build_html(settings: Settings, json_path: Path | None = None) -> Path:
    """渲染 HTML 报告，返回生成的文件路径。

    Args:
        settings: 配置（提供 db_path / report_dir）。
        json_path: 指定输入 JSON；默认取 report_dir 下最新的 *.json。

    Returns:
        生成的 HTML 文件路径。
    """
    report_dir = Path(settings.report_dir)
    if json_path is None:
        candidates = sorted(p for p in report_dir.glob("*.json") if p.stem[:2] == "20")
        if not candidates:
            raise FileNotFoundError(f"{report_dir} 下没有可用的报告 JSON，请先跑一次 main.py")
        json_path = candidates[-1]

    payload = json.loads(Path(json_path).read_text(encoding="utf-8"))
    strategies: dict[str, list[str]] = payload.get("strategies", {})
    data_date = payload.get("data_date", "")
    run_at = payload.get("run_at", "")
    total = payload.get("total", sum(len(v) for v in strategies.values()))

    all_symbols = sorted({s for v in strategies.values() for s in v})
    metrics = _load_metrics(settings.db_path, all_symbols)

    names_path = Path(settings.db_path).parent / "stock_names.json"
    names: dict[str, str] = {}
    if names_path.exists():
        try:
            names = json.loads(names_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"股票名称缓存读取失败：{exc}")

    # 策略按命中数降序，空命中的排最后
    ordered_keys = sorted(strategies, key=lambda k: len(strategies[k]), reverse=True)
    max_count = max((len(v) for v in strategies.values()), default=1) or 1

    overview_rows = "\n".join(
        f'<a class="ov-row{" zero" if not strategies[k] else ""}" href="#s-{k}">'
        f'<span class="ov-label">{escape(STRATEGY_META.get(k, (k, "", "", []))[0])}</span>'
        f'<span class="ov-track"><span class="ov-bar" style="width:{len(strategies[k]) / max_count * 100:.1f}%"></span></span>'
        f'<span class="ov-count">{len(strategies[k])}</span>'
        f"</a>"
        for k in ordered_keys
    )

    sections = "\n".join(
        _render_strategy_section(k, strategies[k], metrics, names) for k in ordered_keys
    )

    run_stamp = run_at  # 直接展示 JSON 里的运行时间，无需变换

    html = _TEMPLATE.format(
        data_date=escape(data_date),
        run_stamp=escape(run_stamp),
        total=total,
        strategy_count=len(strategies),
        symbol_count=len(all_symbols),
        overview_rows=overview_rows,
        sections=sections,
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )

    out = report_dir / f"{json_path.stem}.html"
    out.write_text(html, encoding="utf-8")
    index = report_dir / "index.html"
    index.write_text(html, encoding="utf-8")
    logger.info(f"HTML 看板已生成：{out}")
    return out


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>选股日报 · {data_date}</title>
<style>
:root{{
  /* ── 颜色：纸张暖白 + 红涨绿跌 ── */
  --canvas:#FBFAF7;
  --canvas-soft:#F4F1EA;
  --paper-line:#E3DED3;
  --paper-line-2:#CFC7B7;
  --ink:#1A1815;
  --ink-2:#4A453E;
  --ink-3:#736C63;
  --up:#C0392B;
  --down:#1B7A57;
  --flat:#8A8378;
  --focus:#1F4E79;

  /* ── 圆角只有 2 档 ── */
  --r1:4px;
  --r2:6px;

  /* ── 字阶 1.200：33 / 23 / 16 / 12 ── */
  --fs-display:33px;
  --fs-h2:23px;
  --fs-body:16px;
  --fs-cap:12px;
}}
*{{box-sizing:border-box}}
html{{-webkit-text-size-adjust:100%}}
body{{
  margin:0;
  background:var(--canvas);
  color:var(--ink);
  font:400 var(--fs-body)/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif;
  font-feature-settings:"tnum" 1;
}}
.wrap{{max-width:1200px;margin:0 auto;padding:0 40px}}
a{{color:inherit}}

/* ── 报头 ── */
.masthead{{padding:64px 0 32px;border-bottom:1px solid var(--paper-line-2)}}
.overline{{
  font-size:var(--fs-cap);font-weight:600;letter-spacing:.1em;
  color:var(--ink-3);text-transform:uppercase;margin:0 0 8px;
}}
.masthead h1{{
  font-size:var(--fs-display);font-weight:600;line-height:1.15;
  letter-spacing:-.02em;margin:0 0 12px;
}}
.masthead h1 .date{{font-variant-numeric:tabular-nums}}
.meta{{display:flex;flex-wrap:wrap;gap:4px 16px;margin:0;color:var(--ink-3);font-size:var(--fs-cap)}}
.meta b{{color:var(--ink-2);font-weight:600;font-variant-numeric:tabular-nums}}

/* ── 工具条（sticky） ── */
.toolbar{{
  position:sticky;top:0;z-index:20;background:var(--canvas);
  border-bottom:1px solid var(--paper-line);padding:12px 0;
}}
.toolbar-inner{{display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
.search{{
  flex:0 1 320px;height:44px;
  padding:0 12px;font-size:var(--fs-body);
  font-family:inherit;color:var(--ink);
  background:#fff;border:1px solid var(--paper-line-2);border-radius:var(--r2);
}}
.search::placeholder{{color:var(--ink-3)}}
.search:focus{{outline:none;border-color:var(--focus);box-shadow:0 0 0 3px rgba(31,78,121,.15)}}
.filter-note{{font-size:var(--fs-cap);color:var(--ink-3);font-variant-numeric:tabular-nums}}
.filter-note b{{color:var(--ink);font-weight:600}}

/* ── 概览 ── */
.overview{{padding:32px 0 0}}
.ov-title{{font-size:var(--fs-cap);font-weight:600;letter-spacing:.08em;color:var(--ink-3);margin:0 0 12px}}
.ov-row{{
  display:grid;grid-template-columns:144px 1fr 48px;align-items:center;
  gap:16px;padding:8px 0;text-decoration:none;
  border-bottom:1px solid var(--paper-line);
}}
.ov-row:last-child{{border-bottom:0}}
.ov-label{{font-size:var(--fs-body);color:var(--ink-2);white-space:nowrap}}
.ov-track{{display:block;height:8px;background:var(--canvas-soft);border-radius:var(--r1);overflow:hidden}}
.ov-bar{{display:block;height:100%;background:var(--ink);border-radius:var(--r1)}}
.ov-row.zero .ov-label,.ov-row.zero .ov-count{{color:var(--ink-3)}}
.ov-row.zero .ov-track{{background:transparent;border-top:1px dashed var(--paper-line-2);height:1px}}
.ov-count{{text-align:right;font-variant-numeric:tabular-nums;font-size:var(--fs-body);color:var(--ink)}}
.ov-row:hover .ov-label{{color:var(--ink);font-weight:600}}
.ov-row:hover .ov-bar{{background:var(--focus)}}
.ov-row:focus-visible{{outline:2px solid var(--focus);outline-offset:2px;border-radius:var(--r1)}}

/* ── 策略区块 ── */
main{{padding-bottom:96px}}
.strategy{{padding:32px 0 0}}
.sec-head{{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-bottom:8px}}
.sec-head h2{{
  font-size:var(--fs-h2);font-weight:600;line-height:1.25;
  letter-spacing:-.01em;margin:0;
}}
.sec-count{{font-size:var(--fs-cap);color:var(--ink-3);font-variant-numeric:tabular-nums;white-space:nowrap}}
.sec-count b{{font-size:var(--fs-body);color:var(--ink);font-weight:600}}
.sec-note{{margin:2px 0 8px;max-width:68ch;font-size:var(--fs-body);line-height:1.65;color:var(--ink-2)}}
.sec-cond{{margin:0 0 16px;max-width:92ch;font-size:var(--fs-cap);line-height:1.6;color:var(--ink-3)}}
.sec-usage{{
  margin:0 0 16px;max-width:76ch;padding:12px 16px;
  background:var(--canvas-soft);border-left:2px solid var(--ink);border-radius:var(--r1);
}}
.usage-tag{{display:block;font-size:var(--fs-cap);font-weight:600;letter-spacing:.04em;color:var(--ink-3);margin-bottom:6px}}
.sec-usage ul{{margin:0;padding-left:18px;font-size:var(--fs-body);line-height:1.65;color:var(--ink-2)}}
.sec-usage li{{margin:2px 0}}
.empty{{margin:16px 0 0;padding:16px 0;color:var(--ink-3);font-size:var(--fs-body);border-top:1px solid var(--paper-line)}}

/* ── 表格 ── */
table.picks{{width:100%;border-collapse:collapse;table-layout:fixed;font-variant-numeric:tabular-nums}}
.picks thead th{{
  position:sticky;top:69px;z-index:10;background:var(--canvas);
  font-size:var(--fs-cap);font-weight:500;letter-spacing:.01em;color:var(--ink-3);
  text-align:left;padding:8px 8px;border-bottom:1px solid var(--paper-line-2);
}}
.picks thead th.num{{text-align:right}}
.picks td{{padding:12px 8px;border-bottom:1px solid var(--paper-line);line-height:1.4}}
.picks tbody tr:hover{{background:var(--canvas-soft)}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
th.col-code,td.col-code{{width:104px;font-variant-numeric:tabular-nums}}
th.col-name,td.col-name{{width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.col-name a{{text-decoration:none;border-bottom:1px solid transparent}}
.col-name a:hover{{border-bottom-color:var(--paper-line-2)}}
.col-name a:focus-visible{{outline:2px solid var(--focus);outline-offset:2px}}
.up{{color:var(--up)}}
.down{{color:var(--down)}}
.flat{{color:var(--flat)}}

/* ── 页脚 ── */
footer{{border-top:1px solid var(--paper-line-2);padding:32px 0 64px;margin-top:64px}}
footer p{{margin:0 0 8px;font-size:var(--fs-cap);line-height:1.5;color:var(--ink-3)}}
footer p:last-child{{margin-bottom:0}}

/* ── 断点：640 / 1024 / 1280 ── */
@media (max-width:1279px){{
  .wrap{{padding:0 32px}}
}}
@media (max-width:1023px){{
  .wrap{{padding:0 24px}}
  .masthead{{padding:48px 0 24px}}
  :root{{--fs-display:28px;--fs-h2:21px}}
  .picks thead th{{position:static}}
  th.col-name,td.col-name{{width:200px}}
}}
@media (max-width:639px){{
  .wrap{{padding:0 16px}}
  :root{{--fs-display:26px;--fs-h2:19px}}
  .masthead{{padding:40px 0 24px}}
  main{{padding-bottom:56px}}
  .strategy{{padding:24px 0 0}}
  footer{{margin-top:40px;padding:24px 0 56px}}

  /* 策略分布：标签独占一行，条与数字并排 */
  .ov-row{{grid-template-columns:1fr 48px;gap:8px 12px;padding:12px 0}}
  .ov-label{{grid-column:1/-1}}
  .ov-track{{grid-column:1}}
  .ov-count{{grid-column:2}}

  /* 表格 → 卡片 */
  .picks,.picks tbody,.picks tr,.picks td{{display:block;width:100%}}
  .picks thead{{display:none}}
  .picks tbody tr{{
    display:grid;grid-template-columns:1fr 1fr;gap:12px 16px;
    padding:16px 0;border-bottom:1px solid var(--paper-line);
  }}
  .picks td{{padding:0;border:0;text-align:left}}
  .col-code{{font-size:var(--fs-cap);color:var(--ink-3);letter-spacing:.02em}}
  .col-name{{text-align:right;font-weight:600}}
  .picks td[data-l]{{display:block}}
  .picks td[data-l]::before{{
    content:attr(data-l);display:block;
    font-size:var(--fs-cap);font-weight:400;color:var(--ink-3);margin-bottom:2px;
  }}
  .picks td.num[data-l]{{text-align:left}}
}}
@media print{{
  .toolbar{{display:none}}
  .picks thead th{{position:static}}
  .strategy{{break-inside:avoid}}
}}
</style>
</head>
<body>

<header class="masthead">
  <div class="wrap">
    <p class="overline">Sequoia-X 选股日报</p>
    <h1><span class="date">{data_date}</span> 收盘选股</h1>
    <p class="meta">
      <span>数据截止 <b>{data_date}</b></span>
      <span>运行时间 <b>{run_stamp}</b></span>
      <span>策略 <b>{strategy_count}</b> 个</span>
      <span>命中 <b>{total}</b> 只</span>
      <span>去重 <b>{symbol_count}</b> 只</span>
    </p>
  </div>
</header>

<div class="toolbar">
  <div class="wrap toolbar-inner">
    <input class="search" id="q" type="search" placeholder="搜索代码或名称…" aria-label="搜索代码或名称">
    <span class="filter-note" id="note"></span>
  </div>
</div>

<div class="wrap overview">
  <p class="ov-title">策略分布</p>
  {overview_rows}
</div>

<main class="wrap">
{sections}
</main>

<footer>
  <div class="wrap">
    <p>价格为后复权序列，绝对价位不具备参考意义，故仅展示比率类指标：涨跌幅、20 日涨幅、量比、成交额。</p>
    <p>量比 = 当日成交量 ÷ 前 5 个交易日均量；成交额单位亿元。</p>
    <p>本页由 Sequoia-X 自动生成（{generated}），仅供研究参考，不构成投资建议。</p>
  </div>
</footer>

<script>
(function () {{
  var input = document.getElementById('q');
  var note = document.getElementById('note');
  var rows = Array.prototype.slice.call(document.querySelectorAll('tr[data-code]'));
  var sections = Array.prototype.slice.call(document.querySelectorAll('section.strategy'));
  var total = rows.length;

  function apply() {{
    var q = input.value.trim().toLowerCase();
    var shown = 0;
    rows.forEach(function (tr) {{
      var hit = !q || tr.dataset.code.indexOf(q) > -1 || tr.dataset.name.toLowerCase().indexOf(q) > -1;
      tr.hidden = !hit;
      if (hit) shown++;
    }});
    sections.forEach(function (sec) {{
      var visible = sec.querySelectorAll('tr[data-code]:not([hidden])').length;
      var empty = sec.querySelector('.empty');
      if (q && visible === 0 && !empty) sec.hidden = true;
      else sec.hidden = false;
    }});
    note.innerHTML = q ? '匹配 <b>' + shown + '</b> / ' + total + ' 只' : '';
  }}

  input.addEventListener('input', apply);
  // 概览跳转后给目标区块一个短暂高亮
  document.querySelectorAll('.ov-row').forEach(function (a) {{
    a.addEventListener('click', function () {{
      var t = document.querySelector(a.getAttribute('href'));
      if (!t) return;
      t.style.transition = 'background-color .6s';
      t.style.backgroundColor = 'var(--canvas-soft)';
      setTimeout(function () {{ t.style.backgroundColor = ''; }}, 900);
    }});
  }});
}})();
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="渲染选股结果 HTML 看板")
    parser.add_argument("--json", default=None, help="指定报告 JSON 路径，默认取最新")
    args = parser.parse_args()
    settings = get_settings()
    out = build_html(settings, Path(args.json) if args.json else None)
    print(out)


if __name__ == "__main__":
    main()
