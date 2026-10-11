#!/usr/bin/env python3
"""
EarthMC fall finder: lists towns and nations likely to fall soon.

A town is deleted after 42 days with NO resident logging in, so a town is
"about to fall" when its most recent resident login + 42 days is coming up.
A nation is "inactive" when every one of its towns is about to fall; it falls
when the last of them does. Uses the official API (https://api.earthmc.net/v4).

Usage:
    pip install requests
    python earthmc_fall_finder.py                       # towns/nations falling in next 24h
    python earthmc_fall_finder.py --hours 12
    python earthmc_fall_finder.py --html falling.html   # web page (covers next 14 days)
    python earthmc_fall_finder.py --html falling.html --html-hours 168
    python earthmc_fall_finder.py --webhook https://discord.com/api/webhooks/...
    python earthmc_fall_finder.py --loop 60             # re-check every 60 min
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
session.headers["User-Agent"] = "earthmc-fall-finder/1.1"


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
    """Return (towns, nations) expected to fall within `hours` hours."""
    now = time.time() * 1000
    window = hours * 3600 * 1000
    inactive = INACTIVE_DAYS * 86400 * 1000

    print("Fetching towns...")
    index = call("GET", "/towns")
    all_towns = post_batches(
        "/towns",
        [t["uuid"] for t in index],
        {"name": True, "uuid": True, "mayor": True, "nation": True, "residents": True,
         "status": True, "stats": True},
    )
    ruined = {t["uuid"] for t in all_towns if (t.get("status") or {}).get("isRuined")}
    towns = [t for t in all_towns if t["uuid"] not in ruined]

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

    town_fall = {}   # town uuid -> fall time (ms), or None if unknown
    results, skipped = [], 0
    for t in towns:
        res = t.get("residents", [])
        known = [last_seen[r["uuid"]] for r in res if r["uuid"] in last_seen]
        # Opted-out players are hidden by the API, so we can't be sure
        if not res or len(known) < len(res):
            town_fall[t["uuid"]] = None
            skipped += 1
            continue
        falls_at = max(known) + inactive
        town_fall[t["uuid"]] = falls_at
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

    print("Fetching nations...")
    nindex = call("GET", "/nations")
    nations = post_batches(
        "/nations",
        [n["uuid"] for n in nindex],
        {"name": True, "uuid": True, "king": True, "towns": True, "stats": True},
    )
    nation_results = []
    for n in nations:
        tl = [x["uuid"] for x in n.get("towns", []) if x["uuid"] not in ruined]
        if not tl:
            continue
        falls = [town_fall.get(u) for u in tl]
        # Any town with unknown data, or already past the deadline (likely
        # kept alive by a Premium mayor), means the nation is not going down.
        if any(f is None or f <= now for f in falls):
            continue
        falls_at = max(falls)
        if falls_at <= now + window:
            st = n.get("stats") or {}
            nation_results.append({
                "name": n["name"],
                "king": (n.get("king") or {}).get("name"),
                "towns": st.get("numTowns") or len(tl),
                "residents": st.get("numResidents") or 0,
                "chunks": st.get("numTownBlocks") or 0,
                "balance": round(st.get("balance") or 0),
                "falls_at": falls_at,
                "hours_left": (falls_at - now) / 3600000,
            })
    nation_results.sort(key=lambda x: x["falls_at"])

    print(f"Skipped {skipped} towns with missing/opted-out resident data.")
    return results, nation_results


def fmt(towns, nations, hours):
    towns = [r for r in towns if r["hours_left"] <= hours]
    nations = [r for r in nations if r["hours_left"] <= hours]
    lines = []
    if not towns:
        lines.append(f"No towns expected to fall in the next {hours:g}h.")
    else:
        lines.append(f"Towns expected to fall in the next {hours:g}h ({len(towns)}):")
        for r in towns:
            when = datetime.fromtimestamp(r["falls_at"] / 1000, timezone.utc).strftime("%b %d %H:%M UTC")
            lines.append(
                f"- {r['name']} ({r['nation'] or 'no nation'}) | mayor {r['mayor']} | "
                f"{r['residents']} res, {r['chunks']} chunks, {r['balance']}g | ~{r['hours_left']:.1f}h ({when})"
            )
    lines.append("")
    if not nations:
        lines.append(f"No nations expected to fall in the next {hours:g}h.")
    else:
        lines.append(f"Nations expected to fall in the next {hours:g}h ({len(nations)}):")
        for r in nations:
            when = datetime.fromtimestamp(r["falls_at"] / 1000, timezone.utc).strftime("%b %d %H:%M UTC")
            lines.append(
                f"- {r['name']} | king {r['king']} | {r['towns']} towns, {r['residents']} res, "
                f"{r['chunks']} chunks, {r['balance']}g | ~{r['hours_left']:.1f}h ({when})"
            )
    return "\n".join(lines)


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>EarthMC towns and nations about to fall</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#111;color:#eee}
h1{font-size:1.2rem;margin:0 0 4px} p{margin:0 0 12px;color:#aaa;font-size:.85rem}
.tabs{display:flex;gap:8px;margin-bottom:12px}
.tab{flex:1;padding:10px;background:#222;color:#eee;border:1px solid #444;border-radius:6px;font-size:1rem}
.tab.on{background:#2a4;border-color:#2a4}
.f{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:12px}
.f label{font-size:.75rem;color:#9cf;display:flex;flex-direction:column;gap:3px}
.f div{display:flex;gap:4px}
input,select{width:100%;box-sizing:border-box;padding:6px;background:#222;color:#eee;border:1px solid #444;border-radius:4px}
.reset{padding:6px 12px;background:#444;border:0;color:#fff;border-radius:4px}
.w{overflow-x:auto} table{border-collapse:collapse;width:100%;min-width:640px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #333;font-size:.9rem}
th{color:#9cf;cursor:pointer;position:sticky;top:0;background:#111;white-space:nowrap}
</style></head><body>
<h1>EarthMC towns and nations about to fall</h1>
<p>Data from __UPDATED__ covering the next __HOURS__ hours (rerun the script to refresh). <span id="count"></span></p>
<div class="tabs">
<button class="tab" data-t="towns">Towns</button>
<button class="tab" data-t="nations">Inactive nations</button>
</div>
<div class="f">
<label>Search<input id="q" type="text" placeholder="name, mayor, king..."></label>
<label>Falling within
<select id="win">
<option value="6">6 hours</option><option value="12">12 hours</option>
<option value="24" selected>24 hours</option><option value="48">2 days</option>
<option value="72">3 days</option><option value="168">1 week</option>
<option value="336">2 weeks</option>
</select></label>
<label>Gold in bank<div><input id="gmin" type="number" placeholder="min"><input id="gmax" type="number" placeholder="max"></div></label>
<label>Chunks<div><input id="cmin" type="number" placeholder="min"><input id="cmax" type="number" placeholder="max"></div></label>
<label>Residents<div><input id="rmin" type="number" placeholder="min"><input id="rmax" type="number" placeholder="max"></div></label>
<label class="nat">Towns in nation<div><input id="tmin" type="number" placeholder="min"><input id="tmax" type="number" placeholder="max"></div></label>
<label>&nbsp;<button class="reset" id="reset">Reset filters</button></label>
</div>
<div class="w"><table><thead><tr id="head"></tr></thead><tbody id="rows"></tbody></table></div>
<script>
const DATA = __DATA__;
const ids = ["q","win","gmin","gmax","cmin","cmax","rmin","rmax","tmin","tmax"];
const COLS = {
  towns: [["name","Town"],["nation","Nation"],["mayor","Mayor"],["residents","Residents"],["chunks","Chunks"],["balance","Gold"],["falls_at","Time left"]],
  nations: [["name","Nation"],["king","King"],["towns","Towns"],["residents","Residents"],["chunks","Chunks"],["balance","Gold"],["falls_at","Time left"]]
};
const SEARCH = { towns: ["name","nation","mayor"], nations: ["name","king"] };
const $ = id => document.getElementById(id);
let tab = "towns";
const S = { towns: {k:"falls_at", d:1}, nations: {k:"falls_at", d:1} };
try {
  const s = JSON.parse(localStorage.getItem("emcf2") || "{}");
  ids.forEach(i => { if (s[i] !== undefined) $(i).value = s[i]; });
  if (s.tab === "towns" || s.tab === "nations") tab = s.tab;
} catch (e) {}
if (!$("win").value) $("win").value = "24";
const num = id => { const v = $(id).value; return v === "" ? null : Number(v); };
const esc = s => String(s ?? "-").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const left = h => h <= 0 ? "any moment" : h < 48 ? h.toFixed(1) + "h" : Math.floor(h / 24) + "d " + Math.round(h % 24) + "h";
function render() {
  try { const o = {tab}; ids.forEach(i => o[i] = $(i).value); localStorage.setItem("emcf2", JSON.stringify(o)); } catch (e) {}
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("on", b.dataset.t === tab));
  document.querySelectorAll(".nat").forEach(e => e.style.display = tab === "nations" ? "" : "none");
  const now = Date.now(), q = $("q").value.toLowerCase(), win = Number($("win").value);
  const rng = (v, lo, hi) => (lo === null || v >= lo) && (hi === null || v <= hi);
  const cols = COLS[tab], s = S[tab], all = DATA[tab];
  const rows = all.filter(r =>
    (!q || SEARCH[tab].some(k => String(r[k] ?? "").toLowerCase().includes(q))) &&
    rng(r.balance, num("gmin"), num("gmax")) &&
    rng(r.chunks, num("cmin"), num("cmax")) &&
    rng(r.residents, num("rmin"), num("rmax")) &&
    (tab === "towns" || rng(r.towns, num("tmin"), num("tmax"))) &&
    (r.falls_at - now) / 3600000 <= win
  ).sort((a, b) => {
    const x = a[s.k] ?? "", y = b[s.k] ?? "";
    return (x > y ? 1 : x < y ? -1 : 0) * s.d;
  });
  $("count").textContent = rows.length + " of " + all.length + " " + tab + " in data shown";
  $("head").innerHTML = cols.map(c => `<th data-k="${c[0]}">${c[1]}${s.k === c[0] ? (s.d > 0 ? " ▲" : " ▼") : ""}</th>`).join("");
  document.querySelectorAll("th").forEach(th => th.onclick = () => {
    const k = th.dataset.k; s.d = (s.k === k) ? -s.d : 1; s.k = k; render();
  });
  $("rows").innerHTML = rows.map(r => "<tr>" + cols.map(c => {
    if (c[0] === "falls_at") {
      const when = new Date(r.falls_at).toLocaleString();
      return `<td>${left((r.falls_at - now) / 3600000)} (${esc(when)})</td>`;
    }
    return `<td>${esc(r[c[0]])}</td>`;
  }).join("") + "</tr>").join("") || `<tr><td colspan="${cols.length}">Nothing matches.</td></tr>`;
}
ids.forEach(i => $(i).addEventListener("input", render));
$("reset").onclick = () => { ids.forEach(i => $(i).value = ""); $("win").value = "24"; render(); };
document.querySelectorAll(".tab").forEach(b => b.onclick = () => { tab = b.dataset.t; render(); });
render();
</script></body></html>"""


def write_html(path, towns, nations, hours):
    updated = datetime.now(timezone.utc).strftime("%b %d, %H:%M UTC")
    data = json.dumps({"towns": towns, "nations": nations}).replace("</", "<\\/")
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
    ap.add_argument("--hours", type=float, default=24, help="window for terminal/webhook output")
    ap.add_argument("--html", help="Write results to this HTML file (open it in a browser)")
    ap.add_argument("--html-hours", type=float, default=336,
                    help="how far ahead the web page looks (default 336 = 2 weeks)")
    ap.add_argument("--webhook", help="Discord webhook URL to post results to")
    ap.add_argument("--loop", type=int, help="Re-run every N minutes")
    args = ap.parse_args()

    while True:
        reach = max(args.hours, args.html_hours if args.html else 0)
        towns, nations = find_falling(reach)
        text = fmt(towns, nations, args.hours)
        print(text)
        if args.html:
            write_html(args.html, towns, nations, args.html_hours)
            print(f"Wrote {args.html}")
        if args.webhook:
            send_webhook(args.webhook, text)
        if not args.loop:
            break
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()
    
