#!/bin/bash
# miks-soft — авто-обновление недельного дашборда.
# Показываем только полностью прошедшие недели: каждый день перекачиваем
# последнюю завершённую неделю (Пн–Вс). Текущую, ещё идущую, не трогаем.

set -euo pipefail

D=/home/openclaw/.openclaw/workspace/clients/google/miks-soft/dashboard
PY=/home/openclaw/.openclaw/workspace/google-ads-mcp/venv/bin/python
LOG=/tmp/miks-soft-dashboard-update.log

cd "$D"
exec >>"$LOG" 2>&1
echo "=== $(date -Iseconds) update start ==="

# Пн = 1, Вс = 7 (по ISO). Последняя завершённая неделя = Пн−7 .. Вс−7 от текущей.
DOW=$(date +%u)
CURR_MON=$(date -d "-$(( DOW - 1 )) days" +%Y-%m-%d)
PREV_START=$(date -d "$CURR_MON - 7 days" +%Y-%m-%d)
PREV_END=$(date -d "$PREV_START + 6 days" +%Y-%m-%d)
echo "Refreshing last completed week: $PREV_START .. $PREV_END"

$PY "$D/pull_week.py" "$PREV_START" "$PREV_END" --out "$D/data/week_${PREV_START}.json"

WEEK_START="$PREV_START"
WEEK_END="$PREV_END"

# Пересобираем HTML (внутренняя копия по /dashboards/miks-soft/ обновится сразу)
python3 "$D/build.py"

# Пушим в GH Pages (клиентская копия)
TMP=$(mktemp -d)
git clone -q git@github.com:alex-sterling-lab/miks-soft-dashboard.git "$TMP/repo"
cp "$D/index.html" "$TMP/repo/"
cp "$D/pull_week.py" "$D/build.py" "$TMP/repo/"
cp "$D"/data/*.json "$TMP/repo/data/"
cd "$TMP/repo"

if [ -n "$(git status --porcelain)" ]; then
  git -c user.email=alex.sterling.lab@gmail.com -c user.name="Alex Sterling" add -A
  git -c user.email=alex.sterling.lab@gmail.com -c user.name="Alex Sterling" \
      commit -q -m "Auto-update $(date +%Y-%m-%d) — week ${WEEK_START}..${WEEK_END}"
  git push -q origin master
  echo "pushed"
else
  echo "no changes to push"
fi

rm -rf "$TMP"
echo "=== $(date -Iseconds) update done ==="
