#!/usr/bin/env bash
# 从上游 sngyai/Sequoia-X 同步更新到本地 + 自己的 fork
#
# 用法：
#   ./sync_upstream.sh            # 合并（产生 merge commit，安全，推荐）
#   ./sync_upstream.sh --rebase   # 变基（历史更线性，但会改写本地提交的 hash）
#   ./sync_upstream.sh --check    # 只看有没有新提交，不做任何修改
set -euo pipefail
cd "$(dirname "$0")"

MODE="merge"
case "${1:-}" in
  --rebase) MODE="rebase" ;;
  --check)  MODE="check" ;;
esac

echo "→ 拉取上游 sngyai/Sequoia-X ..."
git fetch upstream --prune --tags

if git merge-base --is-ancestor upstream/master HEAD; then
  echo "✓ 上游没有新提交，已是最新（HEAD: $(git rev-parse --short HEAD)）"
  exit 0
fi

echo "上游有 $(git rev-list --count HEAD..upstream/master) 个新提交："
git log --oneline HEAD..upstream/master | sed 's/^/    /'

if [ "$MODE" = "check" ]; then
  echo "（--check 模式，未做任何修改）"
  exit 0
fi

# 工作区必须干净，否则拒绝
if [ -n "$(git status --porcelain)" ]; then
  echo "✗ 工作区有未提交的改动，请先提交或 stash 后再同步："
  git status --short | sed 's/^/    /'
  exit 1
fi

if [ "$MODE" = "rebase" ]; then
  echo "→ 变基到 upstream/master ..."
  git rebase upstream/master
else
  echo "→ 合并 upstream/master ..."
  if ! git merge --no-edit upstream/master; then
    echo "✗ 出现冲突，需要手动解决："
    echo "    1) 编辑冲突文件，保留你的改动 + 上游改动"
    echo "    2) git add <文件> && git commit"
    echo "    3) ./sync_upstream.sh 重新执行以完成推送"
    exit 1
  fi
fi

echo "→ 推送到你的 fork ..."
git push origin master

echo "✓ 同步完成，当前 HEAD: $(git rev-parse --short HEAD)"
