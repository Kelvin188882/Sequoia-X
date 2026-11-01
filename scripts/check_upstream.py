#!/usr/bin/env python3
"""检查上游 sngyai/Sequoia-X 是否有新提交，并评估与本仓库改动的冲突风险。

严格只读：只做 git fetch / diff / log，不修改工作区、不合并、不推送。

用法：
    .venv/bin/python scripts/check_upstream.py
    .venv/bin/python scripts/check_upstream.py --remote-ref upstream/v1-legacy  # 调试用
    .venv/bin/python scripts/check_upstream.py --write reports/upstream_check.md
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git(*args: str, check: bool = True) -> str:
    """执行 git 命令并返回 stdout。"""
    proc = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="检查上游更新（只读）")
    parser.add_argument("--remote", default="upstream", help="上游远程名（默认 upstream）")
    parser.add_argument("--branch", default="master", help="上游分支（默认 master）")
    parser.add_argument("--remote-ref", default=None, help="直接指定比较用的 ref，调试用")
    parser.add_argument("--write", default=None, help="把报告写入指定文件")
    args = parser.parse_args()

    upstream_ref = args.remote_ref or f"{args.remote}/{args.branch}"
    lines: list[str] = []

    def out(text: str = "") -> None:
        print(text)
        lines.append(text)

    out(f"# 上游更新检查 | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    out()

    # 1. 拉取上游（只更新 remote-tracking 分支，不动工作区）
    try:
        git("fetch", args.remote, "--prune", "--tags")
    except RuntimeError as exc:
        out(f"**拉取失败**：{exc}")
        out()
        out("> 通常是网络/代理问题。本仓库 `.git/config` 中已配置代理；")
        out("> 若在受限环境中运行，需要允许访问 github.com。")
        _write(args.write, lines)
        return 2

    # 2. 计算分叉点与各自的新提交
    try:
        base = git("merge-base", "HEAD", upstream_ref)
    except RuntimeError:
        out(f"**无法比较**：找不到 HEAD 与 {upstream_ref} 的共同祖先")
        _write(args.write, lines)
        return 2

    upstream_new = git("log", "--oneline", f"HEAD..{upstream_ref}").splitlines()
    local_only = git("log", "--oneline", f"{upstream_ref}..HEAD").splitlines()

    out(f"- 比较基准：`{base[:7]}`（分叉点）")
    out(f"- 本地领先上游：{len(local_only)} 个提交")
    out(f"- 上游领先本地：{len(upstream_new)} 个提交")
    out()

    if local_only:
        out("**本地独有提交**（不会被自动覆盖）：")
        for c in local_only:
            out(f"- `{c}`")
        out()

    if not upstream_new:
        out("## 结论：已是最新，无需同步 ✅")
        _write(args.write, lines)
        return 0

    # 3. 上游新提交明细
    out(f"## 上游新提交（{len(upstream_new)} 个）")
    out()
    out("| commit | 日期 | 说明 |")
    out("| --- | --- | --- |")
    log_rows = git(
        "log", "--pretty=format:%h\x1f%ad\x1f%s", "--date=short", f"HEAD..{upstream_ref}"
    ).splitlines()
    for row in log_rows:
        h, d, s = (row.split("\x1f") + ["", "", ""])[:3]
        out(f"| `{h}` | {d} | {s.replace('|', '/')} |")
    out()

    # 4. 文件级改动 + 冲突风险
    our_files = set(git("diff", "--name-only", base, "HEAD").splitlines())
    their_files = set(git("diff", "--name-only", base, upstream_ref).splitlines())
    overlap = sorted(our_files & their_files)

    out(f"## 上游改动涉及 {len(their_files)} 个文件")
    out()
    stat = git("diff", "--stat", base, upstream_ref)
    out("```")
    out(stat)
    out("```")
    out()

    if overlap:
        out(f"## ⚠️ 冲突风险：{len(overlap)} 个文件双方都改过")
        out()
        out("| 文件 | 你的改动 | 上游改动 |")
        out("| --- | --- | --- |")
        for f in overlap:
            mine = _numstat(base, "HEAD", f)
            theirs = _numstat(base, upstream_ref, f)
            out(f"| `{f}` | {mine} | {theirs} |")
        out()
        out("> 这些文件合并时可能冲突，需人工确认保留哪边逻辑。")
    else:
        out("## ✅ 无冲突风险")
        out()
        out("上游改动与本地改动没有文件级重叠，理论上可以自动合并。")

    out()
    out("## 建议")
    out()
    if overlap:
        out("- 逐个看上面重叠文件的 diff，确认上游的改动与你本地改造是否兼容")
        out("- 建议先开分支试合：`git switch -c try-upstream && git merge " + upstream_ref + "`")
        out("- 验证通过（`pytest`）再合回 master，失败直接 `git switch master && git branch -D try-upstream`")
    else:
        out("- 可直接执行 `./sync_upstream.sh`（拉取 + 合并 + 推送到你的 fork）")
    out("- 未确认前**不要盲目合并**：本地有 6 个核心文件被改造过（config/engine/feishu/main/测试）")

    _write(args.write, lines)
    return 0


def _numstat(ref_a: str, ref_b: str, path: str) -> str:
    """把 git diff --numstat 的原始输出格式化成 +N/−M。"""
    raw = git("diff", "--numstat", ref_a, ref_b, "--", path, check=False).strip()
    if not raw:
        return "-"
    parts = raw.split("\t")
    if len(parts) < 2:
        return raw
    add, delete = parts[0], parts[1]
    return f"+{add} / −{delete}"


def _write(path: str | None, lines: list[str]) -> None:
    if not path:
        return
    target = Path(path)
    if not target.is_absolute():
        target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        shown = target.relative_to(ROOT)
    except ValueError:
        shown = target
    print(f"\n（报告已写入 {shown}）")


if __name__ == "__main__":
    sys.exit(main())
