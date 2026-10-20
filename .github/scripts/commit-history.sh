#!/usr/bin/env bash
# 신청 기록 파일을 커밋하고 푸시합니다.
# 스타일씨·무신사 작업이 동시에 푸시할 수 있어서, 충돌하면 최신 내용을 받아 다시 시도합니다.
# 사용: commit-history.sh <기록 파일> <사이트 이름>
set -u
FILE="$1"
SITE="$2"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

if [ ! -f "$FILE" ]; then
  echo "기록 파일 없음: $FILE"
  exit 0
fi

git add "$FILE"
if git diff --cached --quiet; then
  echo "바뀐 기록 없음"
  exit 0
fi
git commit -q -m "$SITE 신청 기록 $(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M')"

for i in 1 2 3 4 5; do
  if git pull -q --rebase origin "$GITHUB_REF_NAME" && git push -q origin "HEAD:$GITHUB_REF_NAME"; then
    echo "기록 저장 완료"
    exit 0
  fi
  echo "푸시 충돌, ${i}번째 다시 시도"
  git rebase --abort 2>/dev/null || true
  sleep $((i * 3))
done
echo "::error::신청 기록을 저장하지 못했습니다."
exit 1
