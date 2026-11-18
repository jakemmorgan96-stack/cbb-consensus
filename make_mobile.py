"""Phone-friendly version of a day's sheet: one card per game instead of a 36-column table.

    python make_mobile.py               # today's sheet
    python make_mobile.py 20261117      # a given day
Writes output/mobile_YYYYMMDD.html next to the regular sheet.
"""
import csv, datetime as dt, html, sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "output"


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    ymd = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().strftime("%Y%m%d")
    src = OUT / f"cbb_consensus_{ymd}.csv"
    rows = list(csv.DictReader(open(src, newline="", encoding="utf-8")))
    day = dt.datetime.strptime(ymd, "%Y%m%d")
    e = lambda s: html.escape(str(s or ""))

    models = [("Bart Torvik", "Torvik"), ("Haslametrics", "Hasla"), ("Massey", "Massey"),
              ("Talisman Red (not in avg)", "Talisman*"), ("Dunkel (not in avg)", "Dunkel*"),
              ("Sasser (not in avg)", "Sasser*")]
    cards, n_watch, n_bet = [], 0, 0
    for r in rows:
        watch = (r.get("Torvik watch (test)") or "").strip()
        bet_s, bet_t = (r.get("Spread bet?") or "").strip(), (r.get("Total bet?") or "").strip()
        n_watch += bool(watch)
        n_bet += bool(bet_s) + bool(bet_t)
        edge = num(r.get("Avg spread edge")) or 0
        tedge = num(r.get("Torvik edge")) or 0
        cls = "card" + (" watch" if watch else "") + (" bet" if bet_s or bet_t else "")
        tags = ""
        if bet_s:
            tags += f'<span class="tag b">SPREAD {e(bet_s)}: {e(r.get("Pick / cover %"))}</span>'
        if bet_t:
            tags += f'<span class="tag b">TOTAL {e(bet_t)}: {e(r.get("Pick / O-U %"))}</span>'
        if watch:
            tags += f'<span class="tag w">Torvik watch: {e(watch)}</span>'
        mrows = ""
        for col, short in models:
            base = col.replace(" (not in avg)", "")
            sp = r.get(col)
            tt = r.get(f"{base} total") or r.get(f"{base} total (not in avg)")
            if not sp and not tt:
                continue
            mt = num(r.get("Market total"))
            diff = f' <small>({num(tt) - mt:+.1f})</small>' if num(tt) is not None and mt is not None else ""
            mrows += f"<tr><td>{short}</td><td>{e(sp) or '–'}</td><td>{e(tt) or '–'}{diff}</td></tr>"
        mline = f'<table class="mt"><tr><th>Model</th><th>Spread</th><th>Total <small>(vs mkt)</small></th></tr>{mrows}</table>' if mrows else ""
        gid = e(f"{r['Away']}@{r['Home']}")
        cards.append(f"""
<div class="{cls}" data-id="{gid}">
  <div class="top"><span class="t">{e(r.get('Start')) or 'TBD'}</span>
    <span class="m">{e(r.get('# models'))} models · SD {e(r.get('Model disagreement (SD)')) or '–'}
    <button class="hide" onclick="toggle(this)">Hide</button></span></div>
  <div class="teams">{e(r['Away'])} <span class="at">@</span> {e(r['Home'])}</div>
  {tags}
  <table>
    <tr><th></th><th>Spread</th><th>Total</th></tr>
    <tr><td>Market</td><td>{e(r.get('Market spread')) or '–'}</td><td>{e(r.get('Market total')) or '–'}</td></tr>
    <tr><td>Open</td><td>{e(r.get('Open spread')) or '–'}</td><td>{e(r.get('Open total')) or '–'}</td></tr>
    <tr><td>Model avg</td><td>{e(r.get('Model avg spread')) or '–'}</td><td>{e(r.get('Model avg total')) or '–'}</td></tr>
    <tr><td>Avg lean</td><td class="{'big' if edge >= 3 else ''}">{e(r.get('Avg spread lean')) or '–'} <small>({e(r.get('Avg spread edge')) or 0})</small></td>
        <td>{e(r.get('Avg total lean')) or '–'} <small>({e(r.get('Avg total edge')) or 0})</small></td></tr>
    <tr><td>Torvik lean</td><td class="{'big' if tedge >= 3 else ''}" colspan="2">{e(r.get('Torvik lean')) or '–'} <small>({e(r.get('Torvik edge')) or 0})</small></td></tr>
  </table>
  <div class="models">{mline}</div>
</div>""")

    page = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CBB {day:%b %-d}</title>
<style>
 body{{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:12px;background:#f4f4f6;color:#1a1a1a}}
 h1{{font-size:20px;margin:4px 0}} .sub{{color:#666;font-size:13px;margin-bottom:12px;line-height:1.4}}
 .card{{background:#fff;border-radius:12px;padding:12px;margin-bottom:10px;box-shadow:0 1px 2px rgba(0,0,0,.08)}}
 .card.watch{{border-left:5px solid #e0a100}} .card.bet{{border-left:5px solid #1f9d55}}
 .top{{display:flex;justify-content:space-between;font-size:12px;color:#777}} .t{{font-weight:600;color:#333}}
 .teams{{font-size:17px;font-weight:600;margin:4px 0 6px}} .at{{color:#999;font-weight:400}}
 .tag{{display:inline-block;font-size:12px;font-weight:600;padding:3px 8px;border-radius:6px;margin:0 4px 6px 0}}
 .tag.w{{background:#fff4d6;color:#7a5600}} .tag.b{{background:#dff5e8;color:#11683a}}
 table{{width:100%;border-collapse:collapse;font-size:14px}} th{{text-align:left;font-size:11px;color:#888;font-weight:500}}
 td{{padding:3px 0;border-top:1px solid #f0f0f0}} td:first-child{{color:#777;width:28%}}
 td.big{{font-weight:700}} small{{color:#999}}
 .models{{margin-top:8px}} table.mt{{font-size:13px}} table.mt td{{padding:2px 0}}
 .hide{{margin-left:8px;font-size:12px;padding:3px 10px;border-radius:12px;border:1px solid #ccc;background:#f7f7f7;color:#444}}
 .card.hidden{{display:none}} body.showall .card.hidden{{display:block;opacity:.45}}
 body.showall .card.hidden .hide{{background:#444;color:#fff}}
 .bar{{position:sticky;top:0;background:inherit;padding:6px 0 8px;z-index:2;display:flex;gap:8px}}
 .bar button{{font-size:13px;padding:6px 12px;border-radius:14px;border:1px solid #ccc;background:#fff;color:#333}}
 @media (prefers-color-scheme:dark){{body{{background:#111;color:#eee}}.card{{background:#1d1d1f}}td{{border-color:#2a2a2a}}
   .t{{color:#ddd}}.tag.w{{background:#3a2e00;color:#ffd666}}.hide,.bar button{{background:#2a2a2c;color:#ddd;border-color:#444}}.tag.b{{background:#0f3321;color:#7ee2a8}}}}
</style></head><body>
<h1>CBB · {day:%a %b %-d}</h1>
<div class="sub">{len(rows)} games · {n_bet} bets · {n_watch} Torvik-watch games (forward test, not bets).<br>
Yellow edge = Torvik watch · green = bet. Edge in (points) vs market; bold = 3+. Model totals show (+/–) vs the market total. * = not in the average.</div>
<div class="bar"><button id="sh" onclick="document.body.classList.toggle('showall');upd()"></button>
<button onclick="if(confirm('Unhide all games?')){{hidden.clear();save();apply()}}">Unhide all</button></div>
{''.join(cards)}
<script>
const KEY = "cbb_hidden_{ymd}";
let hidden = new Set();
try {{ hidden = new Set(JSON.parse(localStorage.getItem(KEY) || "[]")); }} catch (e) {{}}
function save() {{ try {{ localStorage.setItem(KEY, JSON.stringify([...hidden])); }} catch (e) {{}} }}
function upd() {{
  const all = document.body.classList.contains('showall');
  document.getElementById('sh').textContent = (all ? 'Hide hidden' : 'Show hidden') + ' (' + hidden.size + ')';
}}
function apply() {{
  document.querySelectorAll('.card').forEach(c => {{
    const h = hidden.has(c.dataset.id);
    c.classList.toggle('hidden', h);
    c.querySelector('.hide').textContent = h ? 'Unhide' : 'Hide';
  }});
  upd();
}}
function toggle(b) {{
  const id = b.closest('.card').dataset.id;
  hidden.has(id) ? hidden.delete(id) : hidden.add(id);
  save(); apply();
}}
apply();
</script>
</body></html>"""
    dst = OUT / f"mobile_{ymd}.html"
    dst.write_text(page, encoding="utf-8")
    print(f"phone version -> {dst.name}")


if __name__ == "__main__":
    main()
