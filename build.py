#!/usr/bin/env python3
"""Собирает dashboard: читает data/week_*.json, вшивает в index.html."""
import json
import re
from pathlib import Path

D = Path(__file__).parent
weeks = []
for f in sorted((D / "data").glob("week_*.json")):
    weeks.append(json.loads(f.read_text()))
data = {"weeks": weeks, "generated_at": __import__("datetime").datetime.now().isoformat()}

# consolidated data file (доступен в data/all_weeks.json)
(D / "data" / "all_weeks.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))

# inject in html template
tpl = (D / "index.html").read_text()
inline = json.dumps(data, ensure_ascii=False)
new = re.sub(
    r"const DATA = /\*__DATA__\*/[^\n]*",
    f"const DATA = /*__DATA__*/ {inline};",
    tpl,
    count=1,
)
if new == tpl:
    raise SystemExit("Could not inject data — template marker missing")
(D / "index.html").write_text(new)
print(f"Built with {len(weeks)} weeks: {weeks[0]['week']} .. {weeks[-1]['week']}")
