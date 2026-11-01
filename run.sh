#!/usr/bin/env bash
# Sequoia-X 日常运行脚本：增量补数据 + 跑策略 + 生成报告
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p logs

STAMP="$(date +%Y-%m-%d_%H%M%S)"
LOG="logs/run-${STAMP}.log"

echo "[$(date '+%F %T')] Sequoia-X 开始运行" | tee -a "$LOG"
if .venv/bin/python main.py >> "$LOG" 2>&1; then
  echo "[$(date '+%F %T')] 运行成功 → reports/latest.md" | tee -a "$LOG"
  # 只保留最近 30 份日志
  ls -1t logs/run-*.log 2>/dev/null | tail -n +31 | xargs -r rm -f
else
  echo "[$(date '+%F %T')] 运行失败，详见 $LOG" | tee -a "$LOG"
  exit 1
fi
