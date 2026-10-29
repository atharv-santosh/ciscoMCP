"""Build src/tracerlab/data/pt_api.json.gz from the IPC API docs that ship with PT.

    uv run python scripts/build_api_index.py "/Applications/Cisco Packet Tracer 9.0.0"
"""

import gzip
import html
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else "/Applications/Cisco Packet Tracer 9.0.0")
api = next(root.rglob("IpcAPI"))
TAG = re.compile(r"<[^>]+>")


def text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG.sub(" ", s))).strip()


index = {}
for f in sorted(api.glob("class_*.html")):
    if f.stem.endswith("-members"):
        continue
    h = f.read_text(errors="replace")
    m = re.search(r'<div class="title">(.*?) Class Reference</div>', h)
    if not m:
        continue
    cls = text(m.group(1))
    brief = re.search(r'<div class="textblock">(.*?)</div>', h, re.S)
    methods, inherited_from = [], []
    # member rows: <tr class="memitem:..."><td class="memItemLeft">RET</td><td class="memItemRight"><a>name</a> (args)</td></tr>
    # followed by <tr class="memdesc:..."><td class="mdescRight">doc</td>
    rows = re.findall(r'<tr class="memitem:[^"]*"[^>]*>(.*?)</tr>\s*(?:<tr class="memdesc:[^"]*"[^>]*>(.*?)</tr>)?', h, re.S)
    seen = set()
    for row, desc in rows:
        cells = re.findall(r'<td class="memItem(Left|Right)"[^>]*>(.*?)</td>', row, re.S)
        if len(cells) < 2:
            continue
        ret, sig = text(cells[0][1]), text(cells[1][1])
        sig = sig.replace(" (", "(")
        d = text(desc).replace(" More...", "") if desc else ""
        key = ret + sig
        if key in seen:
            continue
        seen.add(key)
        methods.append([ret, sig, d])
    for b in re.findall(r'Public Member Functions inherited from <a [^>]*>(.*?)</a>', h):
        inherited_from.append(text(b))
    index[cls] = {"brief": text(brief.group(1))[:300] if brief else "", "bases": inherited_from, "methods": methods}

out = Path(__file__).resolve().parent.parent / "src/tracerlab/data/pt_api.json.gz"
out.parent.mkdir(exist_ok=True)
with gzip.open(out, "wt") as fh:
    json.dump(index, fh, separators=(",", ":"))
print(f"{len(index)} classes, {sum(len(v['methods']) for v in index.values())} methods -> {out} ({out.stat().st_size // 1024} KB)")
