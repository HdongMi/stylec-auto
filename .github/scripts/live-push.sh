#!/usr/bin/env bash
# 실행 중 진행 상황 파일을 live-<사이트> 브랜치에 올립니다 (대시보드가 15초마다 읽음).
# 매번 커밋 하나짜리 브랜치로 덮어써서 저장소 기록이 쌓이지 않습니다.
#
# 사용: live-push.sh <진행 파일> <사이트> [loop|once]
#   loop: 15초마다 바뀐 내용이 있으면 올림 (백그라운드로 실행)
#   once: 한 번만 올림 (실행이 끝난 뒤 마지막 상태)
set -u
FILE="$1"
SITE="$2"
MODE="${3:-loop}"
INTERVAL="${LIVE_INTERVAL:-15}"
BRANCH="live-$SITE"

export GIT_AUTHOR_NAME="github-actions[bot]" GIT_COMMITTER_NAME="github-actions[bot]"
export GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com"
export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"

last=""
push_live() {
  [ -f "$FILE" ] || return 0
  local hash blob tree commit
  hash=$(sha1sum "$FILE" | cut -d' ' -f1)
  [ "$hash" = "$last" ] && return 0
  blob=$(git hash-object -w "$FILE") || return 0
  tree=$(printf '100644 blob %s\tlive.json\n' "$blob" | git mktree) || return 0
  commit=$(git commit-tree "$tree" -m "live $SITE") || return 0
  if git push -q -f origin "$commit:refs/heads/$BRANCH" 2>/dev/null; then
    last="$hash"
  fi
}

if [ "$MODE" = "once" ]; then
  push_live
  exit 0
fi

while true; do
  push_live
  sleep "$INTERVAL"
done
