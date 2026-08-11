#!/bin/bash
# usage: ./search.sh "Dune"
Q="${1:-Interstellar}"
ENC=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$Q")
curl -s --max-time 200 -X POST localhost:8191/fetch \
  -H 'content-type: application/json' \
  -d "{\"url\":\"https://1337x.to/search/${ENC}/1/\",\"timeout\":150}" \
| python3 -c "
import sys, json, re
try:
    d = json.load(sys.stdin)
except Exception:
    print('minter returned nothing - is it running on :8191?'); raise SystemExit(1)
if 'html' not in d:
    print('minter error:', d.get('detail', d)); raise SystemExit(1)
h = d['html']
title = re.search(r'<title[^>]*>(.*?)</title>', h, re.S)
title = title.group(1).strip() if title else ''
if 'Just a moment' in title:
    print('still challenged - try again'); raise SystemExit(1)
if 'went wrong' in title.lower():
    print('1337x rejected the query (try 3+ characters):', repr('$Q')); raise SystemExit(1)
rows = re.findall(r'href=\"/torrent/[^\"]+\">([^<]+)</a>.*?coll-2 seeds\">(\d+)<.*?coll-4 size[^>]*>([\d.]+ [A-Z]+)', h, re.S)
if not rows:
    print('no results for', repr('$Q')); raise SystemExit(0)
import html as H
for name, seed, size in rows[:15]:
    print(f'{seed:>6}  {size:>9}  {H.unescape(name)[:60]}')
print(f'({len(rows)} results, {d[\"elapsed_ms\"]}ms)')
"
