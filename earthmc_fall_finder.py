#!/usr/bin/env python3
"""
EarthMC fall finder: lists towns likely to fall within the next N hours.

A town is deleted/ruined after 42 days with NO resident logging in, so a town
is "about to fall" when its most recent resident login + 42 days lands inside
the window. Uses the official API (https://api.earthmc.net/v4).

Usage:
    pip install requests
    python earthmc_fall_finder.py                 # next 24h
    python earthmc_fall_finder.py --hours 12
    python earthmc_fall_finder.py --webhook https://discord.com/api/webhooks/...
    python earthmc_fall_finder.py --loop 60       # re-check every 60 min
    python earthmc_fall_finder.py --html falling.html   # also write a web page
"""
import argparse
import json
import time
from datetime import datetime, timezone

import requests

API = "https://api.earthmc.net/v4"
INACTIVE_DAYS = 42      # from earthmc.net/docs/town
BATCH = 100             # lower this if the API rejects big batches
DELAY = 0.4             # ~150 req/min, under the 180/min limit

session = requests.Session()
session.headers["User-Agent"] = "earthmc-fall-finder/1.0"


def call(method, path, **kw):
    for attempt in range(6):
        r = session.request(method, API + path, timeout=30, **kw)
        if r.status_code == 429:  # rate limited, back off
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Rate limited too long, try again later")


def post_batches(path, queries, template):
    out = []
    for i in range(0, len(queries), BATCH):
        out += call("POST", path, json={"query": queries[i:i + BATCH], "template": template})
        time.sleep(DELAY)
    return out


def find_falling(hours):
    now = time.time() * 1000
    window = hours * 3600 * 1000
    inactive = INACTIVE_DAYS * 86400 * 1000

    print("Fetching towns...")
    index = call("GET", "/towns")
    towns = post_batches(
        "/towns",
        [t["uuid"] for t in index],
        {"name": True, "mayor": True, "nation": True, "residents": True,
         "status": True, "stats": True},
    )
    towns = [t for t in towns if not (t.get("status") or {}).get("isRuined")]

    uuids = sorted({r["uuid"] for t in towns for r in t.get("residents", [])})
    print(f"Fetching {len(uuids)} residents (~{len(uuids) // BATCH + 1} requests)...")
    players = post_batches("/players", uuids, {"uuid": True, "timestamps": True, "status": True})

    last_seen = {}
    for p in players:
        if (p.get("status") or {}).get("isNPC"):
            continue
        ts = (p.get("timestamps") or {}).get("lastOnline")
        if (p.get("status") or {}).get("isOnline"):
            ts = now
        if ts:
            last_seen[p["uuid"]] = ts

    results, skipped = [], 0
    for t in towns:
        res = t.get("residents", [])
        known = [last_seen[r["uuid"]] for r in res if r["uuid"] in last_seen]
        # Opted-out players are hidden by the API, so we can't be sure
        if not res or len(known) < len(res):
            skipped += 1
            continue
        falls_at = max(known) + inactive
        if now < falls_at <= now + window:
            results.append({
                "name": t["name"],
                "nation": (t.get("nation") or {}).get("name"),
                "mayor": (t.get("mayor") or {}).get("name"),
                "residents": len(res),
                "chunks": (t.get("stats") or {}).get("numTownBlocks") or 0,
                "balance": round((t.get("stats") or {}).get("balance") or 0),
                "falls_at": falls_at,
                "hours_left": (falls_at - now) / 3600000,
            })
    results.sort(key=lambda x: x["falls_at"])
    print(f"Skipped {skipped} towns with missing/opted-out resident data.")
    return results


def fmt(results, hours):
    if not results:
        return f"No towns expected to fall in the next {hours}h."
    lines = [f"Towns expected to fall in the next {hours}h ({len(results)}):"]
    for r in results:
        when = datetime.fromtimestamp(r["falls_at"] / 1000, timezone.utc).strftime("%b %d %H:%M UTC")
        lines.append(
            f"- {r['name']} ({r['nation'] or 'no nation'}) | mayor {r['mayor']} | "
            f"{r['residents']} res, {r['chunks']} chunks, {r['balance']}g | ~{r['hours_left']:.1f}h ({when})"
        )
    return "\n".join(lines)


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>EarthMC towns about to fall</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#111;color:#eee}
h1{font-size:1.2rem;margin:0 0 4px} p{margin:0 0 12px;color:#aaa;font-size:.85rem}
.f{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:12px}
.f label{font-size:.75rem;color:#9cf;display:flex;flex-direction:column;gap:3px}
.f div{display:flex;gap:4px}
input{width:100%;box-sizing:border-box;padding:6px;background:#222;color:#eee;border:1px solid #444;border-radius:4px}
button{padding:6px 12px;background:#2a4;border:0;color:#fff;border-radius:4px}
.w{overflow-x:auto} table{border-collapse:collapse;width:100%;min-width:640px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #333;font-size:.9rem}
th{color:#9cf;cursor:pointer;position:sticky;top:0;background:#111;white-space:nowrap}
</style></head><body>
<h1>EarthMC towns expected to fall in the next __HOURS__h</h1>
<p>Data from __UPDATED__ (rerun the script to refresh). <span id="count"></span></p>
<div class="f">
<label>Search town / nation / mayor<input id="q" type="text"></label>
<label>Gold in bank<div><input id="gmin" type="number" placeholder="min"><input id="gmax" type="number" placeholder="max"></div></label>
<label>Chunks<div><input id="cmin" type="number" placeholder="min"><input id="cmax" type="number" placeholder="max"></div></label>
<label>Residents<div><input id="rmin" type="number" placeholder="min"><input id="rmax" type="number" placeholder="max"></div></label>
<label>Max hours left<input id="hmax" type="number" placeholder="e.g. 6"></label>
<label>&nbsp;<button id="reset">Reset filters</button></label>
</div>
<div class="w"><table><thead><tr>
<th data-k="name">Town</th><th data-k="nation">Nation</th><th data-k="mayor">Mayor</th>
<th data-k="residents">Residents</th><th data-k="chunks">Chunks</th><th data-k="balance">Gold</th>
<th data-k="falls_at">Time left</th></tr></thead><tbody id="rows"></tbody></table></div>
<script>
const DATA = __DATA__;
const ids = ["q","gmin","gmax","cmin","cmax","rmin","rmax","hmax"];
const $ = id => document.getElementById(id);
let sortKey = "falls_at", sortDir = 1;
try { const saved = JSON.parse(localStorage.getItem("emcf") || "{}"); ids.forEach(i => { if (saved[i]) $(i).value = saved[i]; }); } catch (e) {}
const num = id => { const v = $(id).value; return v === "" ? null : Number(v); };
function render() {
  try { const o = {}; ids.forEach(i => o[i] = $(i).value); localStorage.setItem("emcf", JSON.stringify(o)); } catch (e) {}
  const q = $("q").value.toLowerCase();
  const now = Date.now();
  const inRange = (v, lo, hi) => (lo === null || v >= lo) && (hi === null || v <= hi);
  const rows = DATA.filter(r =>
    (!q || [r.name, r.nation, r.mayor].some(x => (x || "").toLowerCase().includes(q))) &&
    inRange(r.balance, num("gmin"), num("gmax")) &&
    inRange(r.chunks, num("cmin"), num("cmax")) &&
    inRange(r.residents, num("rmin"), num("rmax")) &&
    inRange((r.falls_at - now) / 3600000, null, num("hmax"))
  ).sort((a, b) => {
    const x = a[sortKey] ?? "", y = b[sortKey] ?? "";
    return (x > y ? 1 : x < y ? -1 : 0) * sortDir;
  });
  $("count").textContent = rows.length + " of " + DATA.length + " towns shown";
  const esc = s => String(s ?? "-").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  $("rows").innerHTML = rows.map(r => {
    const h = (r.falls_at - now) / 3600000;
    const left = h <= 0 ? "any moment" : h.toFixed(1) + "h";
    const when = new Date(r.falls_at).toLocaleString();
    return `<tr><td>${esc(r.name)}</td><td>${esc(r.nation)}</td><td>${esc(r.mayor)}</td><td>${r.residents}</td><td>${r.chunks}</td><td>${r.balance}</td><td title="${esc(when)}">${left} (${esc(when)})</td></tr>`;
  }).join("") || '<tr><td colspan="7">No towns match.</td></tr>';
}
ids.forEach(i => $(i).addEventListener("input", render));
$("reset").onclick = () => { ids.forEach(i => $(i).value = ""); render(); };
document.querySelectorAll("th").forEach(th => th.onclick = () => {
  const k = th.dataset.k; sortDir = (sortKey === k) ? -sortDir : 1; sortKey = k; render();
});
render();
</script></body></html>"""


def write_html(path, results, hours):
    updated = datetime.now(timezone.utc).strftime("%b %d, %H:%M UTC")
    data = json.dumps(results).replace("</", "<\\/")
    page = (PAGE.replace("__HOURS__", f"{hours:g}")
                .replace("__UPDATED__", updated)
                .replace("__DATA__", data))
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


def send_webhook(url, text):
    # Discord caps messages at 2000 chars
    for i in range(0, len(text), 1900):
        requests.post(url, json={"content": text[i:i + 1900]}, timeout=15)
        time.sleep(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--webhook", help="Discord webhook URL to post results to")
    ap.add_argument("--html", help="Write results to this HTML file (open it in a browser)")
    ap.add_argument("--loop", type=int, help="Re-run every N minutes")
    args = ap.parse_args()

    while True:
        results = find_falling(args.hours)
        text = fmt(results, args.hours)
        print(text)
        if args.html:
            write_html(args.html, results, args.hours)
            print(f"Wrote {args.html}")
        if args.webhook:
            send_webhook(args.webhook, text)
        if not args.loop:
            break
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()
