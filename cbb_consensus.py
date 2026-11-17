#!/usr/bin/env python3
"""
cbb_consensus.py - pull college basketball game projections from several free
rating sites, line them up game by game, and compare the consensus to the
betting market.

Usage
  python cbb_consensus.py                     # today's games
  python cbb_consensus.py --date 2026-11-08   # a specific day
  python cbb_consensus.py --sites torvik,massey
  python cbb_consensus.py --dump              # only save raw pages (for debugging)
  python cbb_consensus.py --from-cache        # re-parse pages saved earlier, no downloads

Market lines are optional: set ODDS_API_KEY (free key from the-odds-api.com).

Output goes to ./output/: a CSV and an HTML table, sorted so the games where the
models disagree most with the market (or with each other) come first.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import statistics
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
OUT = HERE / "output"

# ---------------------------------------------------------------------------
# Sites. {ymd} = 20261108, {iso} = 2026-11-08.
# "wait_for" is an optional CSS selector the renderer waits for (JS-loaded pages).
# Edit URLs here if a site moves its predictions page.
# ---------------------------------------------------------------------------
SITES = {
    "torvik": {   # table: "McNeese St. at North Texas" | "McNeese St. -1.0 70-69 (54%)"
        "name": "Bart Torvik",
        "url": "https://barttorvik.com/schedule.php?date={ymd}&conlimit=",
        "wait_for": "table tr td",
        "enabled": True,
    },
    "haslametrics": {   # homepage "upcoming games" widget, next 7 days, built by JS
        "name": "Haslametrics",
        "url": "https://haslametrics.com/",
        "wait_for": "#cboUpcomingDates option",
        "enabled": True,
    },
    "massey": {   # table: "Wake Forest @ Vanderbilt" | Pred "72 80" | MOV "-8.5" | Total
        "name": "Massey",
        "url": "https://masseyratings.com/cb/ncaa-d1/games?dt={ymd}",
        "wait_for": "table tr td",
        "enabled": True,
    },
    "talismanred": {   # fixed-width text, next ~3 days
        "name": "Talisman Red",
        "url": "https://talismanred.com/ratings/hoops/predictions.shtml",
        "wait_for": "pre",
        "enabled": True,
        "spread_avg": False,   # weakest on spreads so far; kept in the total average
    },
    "dunkel": {   # one long list of cards, "Load more" button; each card has a date header
        "name": "Dunkel",
        "url": "https://www.dunkelindex.com/picks/ncaa-basketball",
        "wait_for": None,
        "enabled": True,
        "in_consensus": False,   # shown, but left out of the average until tracking says it helps
    },
    "sasser": {   # davidsasser.com/cbb - today's games only, one block per game
        "name": "Sasser",
        "url": "https://davidsasser.com/cbb",
        "wait_for": None,
        "enabled": True,
        "today_only": True,      # the page has no date selector
        "in_consensus": False,   # tracked and calibrated, not in the plain average until it earns it
    },
    "truranking": {   # predictions return Nov 23, 2026 - layout not yet verified
        "name": "TruRanking",
        "url": "https://truranking.com/predictions",
        "wait_for": None,
        "enabled": False,
    },
}

# Runs inside the Haslametrics homepage: picks the date in its "upcoming games"
# box and reads the projected score cells. Returns a JSON list of games.
HASLA_JS = """(d) => {
  const sel = document.getElementById('cboUpcomingDates');
  if (![...sel.options].some(o => o.value == d)) return [];
  sel.value = d;
  document.getElementById('cboUpcomingDatesSort').value = 'Ranking';
  document.getElementById('cboUpcomingDatesList').value = 'Away';
  refreshUpcomingGames();
  const out = [];
  for (const g of [...xmlDoc.getElementsByTagName('tg')].filter(e => e.getAttribute('d') == d)) {
    const k = g.getAttribute('rkr');
    const cell = id => { const e = document.getElementById('tdUpcoming_' + k + '_' + id); return e ? e.innerText.trim() : null; };
    out.push({away: g.getAttribute('t'), home: g.getAttribute('o'), neutral: g.getAttribute('s') != 'A',
              away_pts: cell('1_sc'), home_pts: cell('2_sc')});
  }
  return out;
}"""

SCORE_MIN, SCORE_MAX = 35, 140  # plausible projected team scores


# ---------------------------------------------------------------------------
# Team names
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[’'`]", "", s)
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", "", s)         # drop periods except decimals
    s = re.sub(r"[–—]", "-", s)
    s = re.sub(r"[^a-z0-9().@%-]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class TeamMatcher:
    """Finds D-I team names inside free text, longest alias first."""

    def __init__(self, path: Path):
        alias_to_teams: dict[str, set[str]] = {}
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                a = _norm(row["alias"])
                if len(a) >= 3:
                    alias_to_teams.setdefault(a, set()).add(row["team"])
        # drop aliases that point at more than one team
        self.alias = {a: next(iter(t)) for a, t in alias_to_teams.items() if len(t) == 1}
        alts = sorted(self.alias, key=len, reverse=True)
        self.rx = re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(a) for a in alts) + r")(?![a-z0-9])")

    def find(self, text: str) -> list[tuple[int, int, str]]:
        """Return [(start, end, canonical_team)] in the normalized text."""
        return [(m.start(), m.end(), self.alias[m.group(1)]) for m in self.rx.finditer(text)]


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def fetch(site_key: str, url: str, wait_for: str | None, day: dt.date) -> str:
    """Render the page in headless Chromium (handles JS + simple bot checks)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright missing: pip install playwright && python -m playwright install chromium")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"))
        page.set_default_timeout(30000)
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=15000)
            except Exception:
                pass
        # Torvik can show a short "verifying your browser" page first; give it a moment.
        if "verif" in page.content().lower()[:5000]:
            page.wait_for_timeout(6000)
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=15000)
                except Exception:
                    pass
        if site_key == "haslametrics":
            games = page.evaluate(HASLA_JS, f"{day.month}/{day.day}/{day.year}")
            content = json.dumps(games)
        elif site_key == "dunkel":
            content = dunkel_load(page, day)
        else:
            content = page.content()
        browser.close()
    cache_path(site_key, day).write_text(content, encoding="utf-8")
    return content


def cache_path(site_key: str, day: dt.date) -> Path:
    ext = {"haslametrics": "json", "dunkel": "txt"}.get(site_key, "html")
    return CACHE / f"{site_key}_{day:%Y%m%d}.{ext}"


def dunkel_stamp(day: dt.date) -> str:
    """Card header date as Dunkel writes it, e.g. 'Thursday: 5 November 26'."""
    return f"{day:%A}: {day.day} {day:%B} {day:%y}"


DUNKEL_DATE_RX = re.compile(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday):\s*(\d{1,2}) ([A-Za-z]+)(?: (\d{2}))?", re.M)


def dunkel_card_date(line: str, ref: dt.date) -> dt.date | None:
    """'Friday: 6 November 26 | 13:00' or 'Friday: 6 November | 13:00' (year left
    off for the first cards) -> date. A missing year is taken from `ref`."""
    m = DUNKEL_DATE_RX.match(line.strip())
    if not m:
        return None
    _, d, mon, yy = m.groups()
    try:
        out = dt.datetime.strptime(f"{d} {mon} {yy or ref.strftime('%y')}", "%d %B %y").date()
    except ValueError:
        return None
    if not yy and out < ref - dt.timedelta(days=180):   # Dec->Jan rollover
        out = out.replace(year=out.year + 1)
    return out


def dunkel_load(page, day: dt.date) -> str:
    """Click 'Load more' until a card dated after `day` appears. Waits for new cards
    after each click, stops if nothing new loads twice in a row, and never runs
    longer than ~3 minutes, so a slow Dunkel page can't hold up the whole run."""
    import time as _t
    def cards():
        return [l.strip() for l in page.inner_text("body").splitlines() if DUNKEL_DATE_RX.match(l.strip())]
    start, stalls = _t.time(), 0
    while _t.time() - start < 180:
        cs = cards()
        last = dunkel_card_date(cs[-1], day) if cs else None
        if last and last > day:
            break
        btn = page.locator("#load-more-game")
        try:
            if btn.count() == 0 or not btn.first.is_visible():
                break
            btn.first.click(timeout=10000)
        except Exception:
            break
        for _ in range(20):                      # up to 10 s for new cards
            page.wait_for_timeout(500)
            if len(cards()) > len(cs):
                stalls = 0
                break
        else:
            stalls += 1
            if stalls >= 2:
                break
    return page.inner_text("body")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
@dataclass
class Proj:
    site: str
    teams: tuple[str, str]           # order as seen on the page
    pts: tuple[float, float]         # projected points for teams[0], teams[1]
    home: str | None = None          # home team if the page said "A at B" / "A @ B"
    raw: str = ""


def _rows(page_html: str) -> list[list[str]]:
    """Every table row (as cells) plus every non-table text line (as one cell)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page_html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    rows = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if any(cells):
            rows.append(cells)
        tr.decompose()
    for line in soup.get_text("\n").splitlines():
        line = line.strip()
        if line:
            rows.append([line])
    # <pre> text files (e.g. Talisman Red) come through as lines above.
    return rows


PAIR_RX = re.compile(r"(?<![\d.])(\d{2,3}(?:\.\d)?)\s*[-–]\s*(\d{2,3}(?:\.\d)?)(?![\d.])")
NUM_RX = re.compile(r"^\(?(\d{2,3}(?:\.\d+)?)\)?$")


def _ok(x: float) -> bool:
    return SCORE_MIN <= x <= SCORE_MAX


def parse_row(site: str, cells: list[str], tm: TeamMatcher) -> Proj | None:
    text = " | ".join(cells)
    ntext = _norm(text)
    hits = tm.find(ntext)
    teams = []
    for s, e, t in hits:
        if t not in [x[2] for x in teams]:
            teams.append((s, e, t))
    if len(teams) != 2:
        return None
    (s1, e1, a), (s2, e2, b) = teams
    between = ntext[e1:s2]
    home = b if re.search(r"(^|\s)(at|@)(\s|$)", between) else None
    if "@" in text and home is None:
        home = b

    # 1) "78-70" style pair: the higher score belongs to the team named
    #    closest before the pair (Torvik: "Duke -7.5, 78-70 (75%)").
    for m in PAIR_RX.finditer(ntext):
        x, y = float(m.group(1)), float(m.group(2))
        if not (_ok(x) and _ok(y)):
            continue
        before = [h for h in hits if h[1] <= m.start()]
        if not before:
            continue
        fav = before[-1][2]
        other = b if fav == a else a
        hi, lo = max(x, y), min(x, y)
        # a decimal spread right before the pair ("DePaul -6.8 79-72") is more
        # precise than the rounded scores: keep the total, use the spread.
        sp = re.search(r"-(\d{1,2}\.\d)\s*,?\s*$", ntext[:m.start()])
        if sp and abs(float(sp.group(1)) - (hi - lo)) <= 1.5:
            tot, mg = hi + lo, float(sp.group(1))
            hi, lo = tot / 2 + mg / 2, tot / 2 - mg / 2
        pts = (hi, lo) if fav == a else (lo, hi)
        return Proj(site, (a, b), pts, home, text)

    # 2) separate numeric cells: take the first plausible score after each team.
    if len(cells) > 1:
        team_cell = []
        for t in (a, b):
            idx = next((i for i, c in enumerate(cells) if t in [h[2] for h in tm.find(_norm(c))]), None)
            team_cell.append(idx)
        nums = [(i, float(NUM_RX.match(c.replace(",", "")).group(1)))
                for i, c in enumerate(cells) if NUM_RX.match(c.replace(",", ""))]
        nums = [(i, v) for i, v in nums if _ok(v)]
        if len(nums) >= 2 and None not in team_cell:
            ia, ib = team_cell
            a_only = [v for i, v in nums if ia < i < ib] if ia < ib else []
            b_after = [v for i, v in nums if i > max(ia, ib)]
            if a_only and b_after:            # [A, A_pts, B, B_pts]
                pa, pb = a_only[0], b_after[0]
            else:                             # [A, B, A_pts, B_pts] or both in one cell
                pa, pb = nums[0][1], nums[1][1]
            return Proj(site, (a, b), (pa, pb), home, text)

    # 3) single text line: "Duke 78  Kansas 70" -> number right after each name.
    nums_after = []
    for s, e, t in teams:
        m = re.match(r"\s*\(?(\d{2,3}(?:\.\d)?)\)?(?!\d)", ntext[e:])
        nums_after.append(float(m.group(1)) if m else None)
    if None not in nums_after and all(_ok(v) for v in nums_after):
        return Proj(site, (a, b), (nums_after[0], nums_after[1]), home, text)
    return None


def _team(name: str, tm: TeamMatcher, strip_mascot: bool = True) -> str | None:
    """Whole-string team lookup (no partial matches). If that fails, drop trailing
    words one at a time ('Penn Quakers' -> 'Penn') and try again."""
    n = _norm(name)
    hits = tm.find(n)
    if hits and hits[0][0] == 0 and hits[0][1] == len(n):
        return hits[0][2]
    words = name.split()
    while strip_mascot and len(words) > 1:
        words.pop()
        t = _team(" ".join(words), tm, strip_mascot=False)
        if t:
            return t
    return None


def _from_margin(away: str, home: str, home_margin: float, total: float,
                 neutral: bool, site: str, raw: str) -> Proj:
    hp, ap = total / 2 + home_margin / 2, total / 2 - home_margin / 2
    return Proj(site, (away, home), (ap, hp), None if neutral else home, raw)


def parse_massey(page_html: str, tm: TeamMatcher) -> list[Proj]:
    """Rows: Date | Team | Stand | Scr | Pred | Pwin | MOV | Total.
    Team cell: <a>Away</a><div><a>@ Home</a></div> (no '@' = neutral, venue follows).
    Pred holds both predicted scores; MOV is the favorite's line (always negative).
    MOV/Total cells also hold a hidden '---' placeholder, so read numbers by regex."""
    from bs4 import BeautifulSoup
    num = re.compile(r"-?\d+(?:\.\d+)?")
    out = []
    for tr in BeautifulSoup(page_html, "html.parser").find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 8:
            continue
        links = [a.get_text(" ", strip=True) for a in tds[1].find_all("a")]
        if len(links) < 2:
            continue
        neutral = not links[1].startswith("@")
        away = _team(links[0], tm)
        home = _team(re.sub(r"^@\s*", "", links[1]), tm)
        pred = num.findall(tds[4].get_text(" "))
        mov = num.findall(tds[6].get_text(" "))
        tot = num.findall(tds[7].get_text(" "))
        if not (away and home and len(pred) >= 2 and mov and tot):
            continue
        a_pred, h_pred = float(pred[0]), float(pred[1])
        if a_pred == h_pred:      # tie in rounded scores: fall back to win prob
            wp = num.findall(tds[5].get_text(" "))
            fav_home = len(wp) >= 2 and float(wp[1]) >= float(wp[0])
        else:
            fav_home = h_pred > a_pred
        m = abs(float(mov[0]))
        out.append(_from_margin(away, home, m if fav_home else -m, float(tot[0]),
                                neutral, "massey", tr.get_text(" | ", strip=True)[:200]))
    return out


def parse_talisman(page_text: str, day: dt.date, tm: TeamMatcher) -> list[Proj]:
    """Fixed-width lines: 05-Nov-26  Away  Home  Total  HOME by 28.91.
    The number is always the home-column team's margin (negative = away favored).
    A label other than HOME/AWAY (e.g. 'Mich by') marks a neutral-site game."""
    from bs4 import BeautifulSoup
    pre = BeautifulSoup(page_text, "html.parser").find("pre")
    text = pre.get_text() if pre else page_text
    stamp = f"{day:%d-%b-%y}"
    out = []
    for line in text.splitlines():
        if not line.startswith(stamp):
            continue
        parts = re.split(r"\s{2,}", line.strip())
        m = re.search(r"^(.*?)\s*by\s+(-?\d+(?:\.\d+)?)\s*$", " ".join(parts[4:]))
        if len(parts) < 5 or not m:
            continue
        away, home = _team(parts[1], tm), _team(parts[2], tm)
        if not away or not home:
            continue
        neutral = m.group(1).strip() not in ("HOME", "AWAY")
        out.append(_from_margin(away, home, float(m.group(2)), float(parts[3]),
                                neutral, "talismanred", line))
    return out


def parse_hasla(page_json: str, tm: TeamMatcher) -> list[Proj]:
    out = []
    for g in json.loads(page_json):
        away, home = _team(g["away"], tm), _team(g["home"], tm)
        try:
            ap, hp = float(g["away_pts"]), float(g["home_pts"])
        except (TypeError, ValueError):
            continue
        if away and home:
            out.append(Proj("haslametrics", (away, home), (ap, hp),
                            None if g["neutral"] else home, json.dumps(g)))
    return out


def parse_dunkel(text: str, day: dt.date, tm: TeamMatcher) -> list[Proj]:
    """Page text, one card per game:
        Thursday: 5 November 26 | 15:00   <venue>
        McNeese State Cowboys / RANKING / Dunkel Ranking ... / Dunkel Pick ...
        DUNKEL PREDICTION / Cowboys / Winning Margin: -8 / Total Score: 150
        VEGAS PREDICTION / ... (ignored)
        North Texas Mean Green / RANKING / ...
    Away team is listed first, home second. Margin sign varies, so the
    favorite comes from the mascot named under DUNKEL PREDICTION."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    starts = [i for i, l in enumerate(lines) if DUNKEL_DATE_RX.match(l)]
    out = []
    for k, i in enumerate(starts):
        if dunkel_card_date(lines[i], day) != day:
            continue
        block = lines[i:starts[k + 1] if k + 1 < len(starts) else len(lines)]
        names = [block[j - 1] for j, l in enumerate(block) if l.upper() == "RANKING" and j > 0]
        if len(names) < 2:
            continue
        away_full, home_full = names[0], names[1]
        try:
            dp = next(j for j, l in enumerate(block) if l.upper() == "DUNKEL PREDICTION")
        except StopIteration:
            continue
        seg = block[dp + 1:dp + 8]
        mascot = seg[0] if seg else ""
        m = re.search(r"Winning Margin:\s*(-?\d+(?:\.\d+)?)", " ".join(seg))
        t = re.search(r"Total Score:\s*(\d+(?:\.\d+)?)", " ".join(seg))
        if not (m and t and mascot):
            continue
        def team(full):
            h = tm.find(_norm(full))
            if h and h[0][0] == 0:
                return h[0][2]
            words = full.split()          # drop the mascot word by word
            while len(words) > 1:
                words.pop()
                t = _team(" ".join(words), tm)
                if t:
                    return t
            return None
        away, home = team(away_full), team(home_full)
        if not away or not home:
            continue
        nm = _norm(mascot)
        a_hit, h_hit = _norm(away_full).endswith(nm), _norm(home_full).endswith(nm)
        if a_hit == h_hit:          # both or neither (same mascot) - can't tell
            continue
        margin = abs(float(m.group(1)))
        out.append(_from_margin(away, home, margin if h_hit else -margin, float(t.group(1)),
                                False, "dunkel", " | ".join(block[:3])))
    return out


def parse_sasser(page_html: str, tm: TeamMatcher) -> list[Proj]:
    """davidsasser.com/cbb: one block per game headed 'Away at Home', with a
    'Projected score' (away, home, one decimal each), 'Model spread' and
    'Projected total'. Uses the projected scores; falls back to model spread +
    projected total if the scores can't be read."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page_html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    lines = [l.strip() for l in soup.get_text("\n").splitlines() if l.strip()]
    heads = []
    for i, l in enumerate(lines):
        m = re.match(r"^(.+?)\s+(?:at|@)\s+(.+?)$", l)
        if m and len(l) < 80:
            a, h = _team(m.group(1), tm), _team(m.group(2), tm)
            if a and h and a != h:
                heads.append((i, a, h))
    out, seen = [], set()
    for k, (i, away, home) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        block = lines[i + 1:end]
        text = " | ".join(block)
        if frozenset((away, home)) in seen:
            continue
        pts = None
        j = next((n for n, l in enumerate(block) if re.search(r"projected score", l, re.I)), None)
        if j is not None:
            nums = re.findall(r"(?<![\d.%])(\d{2,3}\.\d)(?![\d%])", " | ".join(block[j:j + 8]))
            vals = [float(x) for x in nums if 35 <= float(x) <= 140]
            if len(vals) >= 2:
                pts = (vals[0], vals[1])
        if pts is None:      # fallback: model spread + projected total
            ms = re.search(r"model spread[^A-Za-z]*([A-Za-z].+?)\s*([−-]\d+(?:\.\d)?|\+\d+(?:\.\d)?|PK)", text, re.I)
            tt = re.search(r"projected total[^\d]*(\d{2,3}(?:\.\d)?)", text, re.I)
            if ms and tt:
                fav = _team(ms.group(1).strip(" |:"), tm)
                v = ms.group(2).replace("−", "-")
                spread = 0.0 if v == "PK" else float(v)
                total = float(tt.group(1))
                home_margin = -spread if fav == home else spread if fav == away else None
                if home_margin is not None:
                    seen.add(frozenset((away, home)))
                    out.append(_from_margin(away, home, home_margin, total, False, "sasser", text[:200]))
            continue
        seen.add(frozenset((away, home)))
        out.append(Proj("sasser", (away, home), pts, home, text[:200]))
    return out


def parse_site(site: str, page: str, tm: TeamMatcher, day: dt.date) -> list[Proj]:
    if site == "sasser":
        return parse_sasser(page, tm)
    if site == "dunkel":
        return parse_dunkel(page, day, tm)
    if site == "massey":
        return parse_massey(page, tm)
    if site == "talismanred":
        return parse_talisman(page, day, tm)
    if site == "haslametrics":
        return parse_hasla(page, tm)
    return parse_generic(site, page, tm)


def parse_generic(site: str, page_html: str, tm: TeamMatcher) -> list[Proj]:
    out, seen = [], set()
    for cells in _rows(page_html):
        p = parse_row(site, cells, tm)
        if p:
            key = frozenset(p.teams)
            if key not in seen:
                seen.add(key)
                out.append(p)
    return out


# ---------------------------------------------------------------------------
# Market lines (optional)
# ---------------------------------------------------------------------------
def odds_api_lines(day: dt.date, tm: TeamMatcher) -> dict[frozenset, dict]:
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        return {}
    import requests
    r = requests.get(
        "https://api.the-odds-api.com/v4/sports/basketball_ncaab/odds",
        params={"apiKey": key, "regions": "us", "markets": "spreads,totals",
                "oddsFormat": "american"}, timeout=30)
    r.raise_for_status()
    local_tz = dt.datetime.now().astimezone().tzinfo
    lines = {}
    for g in r.json():
        start = dt.datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00")).astimezone(local_tz)
        if start.date() != day:
            continue
        h = tm.find(_norm(g["home_team"]))
        aw = tm.find(_norm(g["away_team"]))
        if not h or not aw:
            continue
        home, away = h[0][2], aw[0][2]
        spreads, totals = [], []
        for bk in g.get("bookmakers", []):
            for mk in bk.get("markets", []):
                for o in mk.get("outcomes", []):
                    if mk["key"] == "spreads" and tm.find(_norm(o["name"])) and tm.find(_norm(o["name"]))[0][2] == home:
                        spreads.append(o["point"])
                    if mk["key"] == "totals" and o["name"] == "Over":
                        totals.append(o["point"])
        lines[frozenset((home, away))] = {
            "home": home, "away": away, "start": start,
            "home_spread": statistics.median(spreads) if spreads else None,
            "total": statistics.median(totals) if totals else None,
        }
    return lines


ESPN_URL = ("https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/"
            "scoreboard?dates={ymd}&groups=50&limit=400")


def espn_lines(day: dt.date, tm: TeamMatcher) -> dict[frozenset, dict]:
    """Free, no key: ESPN's public scoreboard feed carries DraftKings opening and
    current spreads/totals for most D-I games."""
    import requests
    r = requests.get(ESPN_URL.format(ymd=f"{day:%Y%m%d}"), timeout=30,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    local_tz = dt.datetime.now().astimezone().tzinfo
    def num(x):
        try:
            return float(str(x).lstrip("ou").replace("+", ""))
        except (TypeError, ValueError):
            return None
    lines = {}
    for ev in r.json().get("events", []):
        comp = ev["competitions"][0]
        teams = {c["homeAway"]: c["team"].get("displayName", "") for c in comp["competitors"]}
        home, away = _team(teams.get("home", ""), tm), _team(teams.get("away", ""), tm)
        odds = (comp.get("odds") or [{}])[0]
        if not home or not away or odds.get("spread") is None:
            continue
        ps, tot = odds.get("pointSpread", {}), odds.get("total", {})
        start = dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(local_tz)
        lines[frozenset((home, away))] = {
            "home": home, "away": away, "start": start, "neutral": comp.get("neutralSite", False),
            "home_spread": num(odds.get("spread")), "total": num(odds.get("overUnder")),
            "open_spread": num(ps.get("home", {}).get("open", {}).get("line")),
            "open_total": num(tot.get("over", {}).get("open", {}).get("line")),
            "book": (odds.get("provider") or {}).get("name", ""),
        }
    return lines


def market_lines(day: dt.date, tm: TeamMatcher) -> dict[frozenset, dict]:
    """ESPN feed by default; The Odds API only if ESPN fails and a key is set."""
    try:
        lines = espn_lines(day, tm)
        if lines:
            return lines
    except Exception as e:
        print(f"  ESPN lines failed: {e}")
    return odds_api_lines(day, tm)


def in_avg(site: str, kind: str) -> bool:
    """Does `site` count toward the simple consensus average for 'spread' or 'total'?"""
    cfg = SITES[site]
    if not cfg.get("in_consensus", True):
        return False
    return cfg.get(f"{kind}_avg", True)


def safe_write(path: Path, write_fn) -> Path:
    """Write a file, replacing any earlier version (so re-running a day just
    overwrites it). If Windows has the file locked - usually because it's open
    in Excel - save next to it with the time added instead of crashing."""
    try:
        write_fn(path)
        return path
    except PermissionError:
        alt = path.with_name(f"{path.stem}_{dt.datetime.now():%H%M%S}{path.suffix}")
        write_fn(alt)
        print(f"  NOTE: {path.name} is open in another program (Excel?) - saved as {alt.name}")
        return alt


# ---------------------------------------------------------------------------
# Calibration: how much of each model's disagreement with the market to trust
# ---------------------------------------------------------------------------
WEIGHTS_FILE = OUT / "model_weights.json"
BET_PROB = 0.545          # flag a bet only when the cover / over-under chance is at least this
WATCH_START = "2026-11-14"     # Torvik watch rule frozen on this date; only later games test it
WATCH_GAP = (3.0, 5.0)         # Torvik vs opening line, points
WATCH_MAX_LINE = 20.0          # skip games with a market spread of 20+ (big gaps on huge favorites lose)
MIN_GAMES_FOR_BETS = 300  # before this many graded games, strong picks show as "lean", not "BET"
LINE_MOVE_SKIP = 2.5      # skip when the market moved this many points against the pick since opening
HALF_LIFE_DAYS = 10       # recent games count more (models improve as preseason priors fade)
DEFAULT_SIGMA = {"spread": 10.5, "total": 15.0}


def _phi(z: float) -> float:
    import math
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _ridge(X, y, w, lam):
    """Weighted ridge fit with weights kept >= 0: a model's disagreement with the
    market can be trusted or ignored, but never bet against. Models whose weight
    comes out negative are dropped and the rest refitted."""
    import numpy as np
    keep = list(range(X.shape[1]))
    b = np.zeros(X.shape[1])
    while keep:
        Xk = X[:, keep]
        A = Xk.T @ (Xk * w[:, None]) + lam * np.eye(len(keep))
        bk = np.linalg.solve(A, Xk.T @ (y * w))
        if (bk >= 0).all():
            b[:] = 0
            b[keep] = bk
            break
        keep = [k for k, v in zip(keep, bk) if v > 0]
    return b


def _logit_fit(X, y, w, lam):
    """Weighted ridge logistic regression with no intercept (0 disagreement = 50%)
    and coefficients kept >= 0: models are trusted or ignored, never bet against."""
    import numpy as np
    keep = list(range(X.shape[1]))
    b = np.zeros(X.shape[1])
    while keep:
        Xk, bk = X[:, keep], np.zeros(len(keep))
        for _ in range(50):                     # Newton steps
            p = 1 / (1 + np.exp(-(Xk @ bk)))
            g = Xk.T @ (w * (p - y)) + lam * bk
            H = Xk.T @ (Xk * (w * p * (1 - p))[:, None]) + lam * np.eye(len(keep))
            step = np.linalg.solve(H, g)
            bk -= step
            if np.abs(step).max() < 1e-7:
                break
        if (bk >= 0).all():
            b[:] = 0
            b[keep] = bk
            break
        keep = [k for k, v in zip(keep, bk) if v > 0]
    return b


def calibrate(sites: list[str], min_games: int = 60) -> dict:
    """Fit, from output/results_log.csv, how each model's disagreement with the
    morning market line relates to whether the bet actually covered (spreads) or
    went over/under (totals), and save model_weights.json.

    cover chance = logistic( sum_s weight_s * clip(model_s - market, +-cap) )

    - Target is the bet result itself (pushes dropped), not prediction accuracy.
    - Weights >= 0, shrunk toward 0; shrinkage and the cap on how much a single
      big disagreement can count (big gaps have been losing) are picked by 5-fold
      cross-validation on log-loss, with recent games counting more.
    - A model only gets weight if, out-of-sample, it beats the coin-flip baseline
      AND its strong picks (>= BET_PROB) would have won more than 52.4% (break-even
      at -110). Otherwise every weight is 0 and the sheet says "no edge"."""
    import numpy as np
    log = OUT / "results_log.csv"
    src = log_file()
    blank = {"weights": {}, "cap": None, "sigma": None}
    out = {"updated": dt.date.today().isoformat(), "n": 0, "method": "cover",
           "spread": dict(blank), "total": dict(blank, bias={})}
    if not src.exists():
        return out
    with open(src, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("home_score") and r.get("mkt_line")]
    out["n"] = len(rows)
    if len(rows) < min_games:
        safe_write(WEIGHTS_FILE, lambda p: p.write_text(json.dumps(out, indent=2)))
        return out
    f_ = lambda v: float(v) if v not in ("", None) else np.nan
    today = dt.date.today()
    age = np.array([(today - dt.date.fromisoformat(r["date"])).days for r in rows], float)
    rec_w = 0.5 ** (age / HALF_LIFE_DAYS)
    hs = np.array([f_(r["home_score"]) for r in rows]); as_ = np.array([f_(r["away_score"]) for r in rows])
    ln2 = float(np.log(2))

    def fit(kind):
        if kind == "spread":
            line = np.array([f_(r["mkt_line"]) for r in rows])
            base, res = -line, (hs - as_) + line                    # res > 0: home covered
            model = {s: np.array([-f_(r.get(f"{s}_line")) for r in rows]) for s in sites}
        else:
            base = np.array([f_(r.get("mkt_total")) for r in rows])
            res = (hs + as_) - base                                  # res > 0: over
            model = {s: np.array([f_(r.get(f"{s}_total")) for r in rows]) for s in sites}
        ok = ~np.isnan(base) & ~np.isnan(res) & (res != 0)
        use = [s for s in sites if (~np.isnan(model[s]) & ok).sum() >= 50]
        bias = {}
        if kind == "total":
            for s in use:
                m = ~np.isnan(model[s]) & ok
                e = model[s][m] - (base[m] + res[m])
                bias[s] = round(float(e.mean() * m.sum() / (m.sum() + 50)), 2)
        D = np.column_stack([np.nan_to_num(model[s] - bias.get(s, 0) - base, nan=0.0) for s in use])[ok] \
            if use else np.zeros((ok.sum(), 0))
        y, w = (res[ok] > 0).astype(float), rec_w[ok]
        result = {"weights": {}, "bias": bias, "cap": None, "n": int(ok.sum()),
                  "baseline_logloss": round(ln2, 4)}
        if not use:
            return result
        idx = np.arange(len(y)); rng = np.random.default_rng(0); rng.shuffle(idx)
        folds = np.array_split(idx, 5)
        best = None
        for cap in (3, 5, 8, 99):
            X = np.clip(D, -cap, cap) / 10
            for lam in (0.3, 1, 3, 10, 30):
                pr = np.full(len(y), 0.5)
                for fo in folds:
                    tr = np.setdiff1d(idx, fo)
                    b = _logit_fit(X[tr], y[tr], w[tr], lam)
                    pr[fo] = 1 / (1 + np.exp(-(X[fo] @ b)))
                pc = np.clip(pr, 1e-6, 1 - 1e-6)
                ll = float(np.average(-(y * np.log(pc) + (1 - y) * np.log(1 - pc)), weights=w))
                strong = np.maximum(pr, 1 - pr) >= BET_PROB
                hit = float(((pr >= 0.5) == (y == 1))[strong].mean()) if strong.sum() >= 30 else None
                if best is None or ll < best["ll"]:
                    best = {"ll": ll, "cap": cap, "lam": lam, "hit": hit, "n_strong": int(strong.sum())}
        result.update(cv_logloss=round(best["ll"], 4), cv_strong_picks=best["n_strong"],
                      cv_strong_hit=None if best["hit"] is None else round(best["hit"], 3))
        if best["ll"] < ln2 - 0.002 and best["hit"] is not None and best["hit"] > 0.524:
            X = np.clip(D, -best["cap"], best["cap"]) / 10
            b = _logit_fit(X, y, w, best["lam"]) / 10                # per point of disagreement
            result["weights"] = {s: round(float(v), 4) for s, v in zip(use, b) if v > 0}
            result["cap"] = best["cap"]
        return result

    out["spread"], out["total"] = fit("spread"), fit("total")
    OUT.mkdir(exist_ok=True)
    safe_write(WEIGHTS_FILE, lambda p: p.write_text(json.dumps(out, indent=2)))
    return out


def load_weights() -> dict:
    try:
        return json.loads(WEIGHTS_FILE.read_text())
    except Exception:
        return {"n": 0, "spread": {"weights": {}}, "total": {"weights": {}, "bias": {}}}


def _ppf(p: float) -> float:
    """Inverse standard normal (for turning a cover chance back into points)."""
    import math
    lo, hi = -8.0, 8.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if _phi(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def blend(g, mk: dict, W: dict) -> dict:
    """Cover / over-under chance from the calibrated weights, the side it favors,
    a bet flag, and a 'blended' line for display (market moved by the implied edge)."""
    import math
    out = {}
    def chance(kind, preds, base):
        Wk = W.get(kind, {})
        wts, cap, bias = Wk.get("weights", {}), Wk.get("cap") or 99, Wk.get("bias", {})
        z = sum(wt * max(-cap, min(cap, preds[s] - bias.get(s, 0) - base)) for s, wt in wts.items() if s in preds)
        return 1 / (1 + math.exp(-z))
    def flag_for(prob, moved_against):
        if prob < BET_PROB:
            return ""
        if moved_against:
            return "skip: line moved"
        return "BET" if W.get("n", 0) >= MIN_GAMES_FOR_BETS else "lean"
    ml, mt = mk.get("home_spread"), mk.get("total")
    if ml is not None:
        p_home = chance("spread", g.margins, -ml)
        side = "home" if p_home >= 0.5 else "away"
        prob = max(p_home, 1 - p_home)
        op = mk.get("open_spread")
        moved = (ml - op) if op is not None else 0           # negative = moved toward home
        against = (moved > 0 if side == "home" else moved < 0) and abs(moved) >= LINE_MOVE_SKIP
        shift = DEFAULT_SIGMA["spread"] * _ppf(p_home)        # points of edge for display
        out.update(blend_margin=round(-ml + shift, 1), bspread_side=side, bspread_prob=round(prob, 3),
                   bspread_flag=flag_for(prob, against))
    if mt is not None:
        p_over = chance("total", g.totals, mt)
        side = "over" if p_over >= 0.5 else "under"
        prob = max(p_over, 1 - p_over)
        ot = mk.get("open_total")
        moved = (mt - ot) if ot is not None else 0
        against = (moved < 0 if side == "over" else moved > 0) and abs(moved) >= LINE_MOVE_SKIP
        shift = DEFAULT_SIGMA["total"] * _ppf(p_over)
        out.update(blend_total=round(mt + shift, 1), btotal_side=side, btotal_prob=round(prob, 3),
                   btotal_flag=flag_for(prob, against))
    return out


# ---------------------------------------------------------------------------
# Combine
# ---------------------------------------------------------------------------
@dataclass
class Game:
    teams: frozenset
    home: str | None = None
    away: str | None = None
    margins: dict = field(default_factory=dict)   # site -> projected home(or team1) margin
    totals: dict = field(default_factory=dict)
    market: dict | None = None


def combine(projs: list[Proj], market: dict) -> list[Game]:
    games: dict[frozenset, Game] = {}
    for p in projs:
        k = frozenset(p.teams)
        g = games.setdefault(k, Game(k))
        if p.home and not g.home:
            g.home = p.home
    for k, m in market.items():
        if len(k) < 2:
            print(f"  WARNING market: skipped a game where both teams read as {m.get('home')!r}")
            continue
        g = games.setdefault(k, Game(k))
        g.home, g.market = m["home"], m
    for g in games.values():
        if not g.home:
            g.home = sorted(g.teams)[1]          # unknown venue: arbitrary but stable
        g.away = next(t for t in g.teams if t != g.home)
    for p in projs:
        g = games[frozenset(p.teams)]
        pts = dict(zip(p.teams, p.pts))
        g.margins[p.site] = round(pts[g.home] - pts[g.away], 1)
        g.totals[p.site] = round(pts[g.home] + pts[g.away], 1)
    return list(games.values())


def fill_from_earlier(games: list[Game], day: dt.date) -> int:
    """Re-running later in the day (e.g. a GitHub run that starts late, after tip-off) finds
    games already underway: ESPN drops their lines and some sites drop them. Keep whatever the
    earlier run saw for anything that is now missing, so a re-run never makes the sheet worse."""
    raw = OUT / f"raw_{day:%Y%m%d}.csv"
    if not raw.exists():
        return 0
    with open(raw, newline="", encoding="utf-8") as f:
        prev = list(csv.DictReader(f))
    byk = {g.teams: g for g in games}
    filled = 0
    f = lambda x: float(x) if x not in ("", None) else None
    for r in prev:
        k = frozenset((r["away"], r["home"]))
        if len(k) < 2:
            continue
        g = byk.get(k)
        if g is None:
            g = Game(k, home=r["home"], away=r["away"])
            games.append(g)
            byk[k] = g
        sgn = -1 if g.home != r["home"] else 1
        for s in SITES:
            ln, tt = f(r.get(f"{s}_line")), f(r.get(f"{s}_total"))
            if s not in g.margins and ln is not None:
                g.margins[s] = round(-ln * sgn, 1)
                filled += 1
            if s not in g.totals and tt is not None:
                g.totals[s] = tt
        m = dict(g.market or {})
        m["home"] = g.home
        changed = False
        for key, col, signed in (("home_spread", "mkt_line", True), ("total", "mkt_total", False),
                                 ("open_spread", "open_line", True), ("open_total", "open_total", False)):
            v = f(r.get(col))
            if m.get(key) is None and v is not None:
                m[key] = v * sgn if signed else v
                changed = True
        if changed:
            g.market = m
            filled += 1
    return filled


def summarize(g: Game, sites: list[str], W: dict | None = None) -> dict:
    # Sites flagged out of the average (see in_avg) are still shown for reference.
    ms = [g.margins[s] for s in g.margins if in_avg(s, "spread")]
    ts = [g.totals[s] for s in g.totals if in_avg(s, "total")]
    row = {"away": g.away, "home": g.home, "n_models": len(ms)}
    for s in sites:
        m = g.margins.get(s)
        row[f"{s}_line"] = "" if m is None else -m        # as a home spread, e.g. -6.5
        row[f"{s}_total"] = g.totals.get(s, "")
    row["cons_line"] = round(-statistics.mean(ms), 1) if ms else ""
    row["cons_total"] = round(statistics.mean(ts), 1) if ts else ""
    row["model_spread_sd"] = round(statistics.stdev(ms), 1) if len(ms) > 1 else ""
    mk = g.market or {}
    row["mkt_line"] = mk.get("home_spread", "") if mk.get("home_spread") is not None else ""
    row["mkt_total"] = mk.get("total", "") if mk.get("total") is not None else ""
    row["open_line"] = mk.get("open_spread") if mk.get("open_spread") is not None else ""
    row["open_total"] = mk.get("open_total") if mk.get("open_total") is not None else ""
    row["line_edge"] = round(row["mkt_line"] - row["cons_line"], 1) if row["mkt_line"] != "" and ms else ""
    row["total_edge"] = round(row["cons_total"] - row["mkt_total"], 1) if row["mkt_total"] != "" and ts else ""
    e = row["line_edge"]
    row["lean"] = "" if e in ("", 0) else (
        f"{g.home} {row['mkt_line']:+g}" if e > 0 else f"{g.away} {-row['mkt_line']:+g}")
    t = row["total_edge"]
    row["total_lean"] = "" if t in ("", 0) else f"{'Over' if t > 0 else 'Under'} {row['mkt_total']:g}"
    # Torvik on its own (the best single model so far)
    tl = row.get("torvik_line", "")
    row["torvik_edge"] = round(row["mkt_line"] - tl, 1) if tl != "" and row["mkt_line"] != "" else ""
    te = row["torvik_edge"]
    row["torvik_edge_pts"] = abs(te) if te != "" else ""
    row["torvik_lean"] = "" if te in ("", 0) else (
        f"{g.home} {row['mkt_line']:+g}" if te > 0 else f"{g.away} {-row['mkt_line']:+g}")
    # Torvik watch: moderate Torvik gap vs the OPENING line, not a huge favorite.
    # Found in data through Nov 13; tracked from Nov 14 on to see if it holds up.
    ol = row.get("open_line", "")
    row["torvik_watch"] = ""
    if tl != "" and ol != "" and row["mkt_line"] != "" and abs(row["mkt_line"]) < WATCH_MAX_LINE:
        gap = ol - tl                                  # >0: Torvik likes home more than the opener
        if WATCH_GAP[0] <= abs(gap) < WATCH_GAP[1]:
            row["torvik_watch"] = (f"{g.home} {row['mkt_line']:+g}" if gap > 0 else f"{g.away} {-row['mkt_line']:+g}") \
                + " (opened " + (f"{g.home} {ol:g}" if ol < 0 else f"{g.away} {-ol:g}" if ol > 0 else "PK") + ")"
    # Blended prediction from the calibrated weights
    b = blend(g, mk, W or load_weights())
    row.update(b)
    row["blend_line"] = -b["blend_margin"] if "blend_margin" in b else ""
    if "bspread_side" in b:
        if b["bspread_prob"] < 0.505:
            row["cover_pct"] = "no edge"
            row["bspread_side"] = ""          # nothing to grade
        else:
            team, line = (g.home, row["mkt_line"]) if b["bspread_side"] == "home" else (g.away, -row["mkt_line"])
            row["cover_pct"] = f"{team} {line:+g}  {b['bspread_prob']:.0%}"
    if "btotal_side" in b:
        if b["btotal_prob"] < 0.505:
            row["ou_pct"] = "no edge"
            row["btotal_side"] = ""
        else:
            row["ou_pct"] = f"{b['btotal_side'].title()} {row['mkt_total']:g}  {b['btotal_prob']:.0%}"
    st = mk.get("start")
    row["_start"] = st
    row["start"] = f"{(st.hour % 12) or 12}:{st.minute:02d} {'AM' if st.hour < 12 else 'PM'}" if st else ""
    return row


# Output columns: (key, header). Spread block, total block, then supporting detail.
def out_columns(sites):
    names = {s: SITES[s]["name"] for s in sites}
    def tag(s, kind):
        return "" if in_avg(s, kind) else " (not in avg)"
    return ([("start", "Start"), ("away", "Away"), ("home", "Home"),
             ("blend_line", "Blended spread"), ("mkt_line", "Market spread"), ("cover_pct", "Pick / cover %"), ("bspread_flag", "Spread bet?"),
             ("blend_total", "Blended total"), ("mkt_total", "Market total"), ("ou_pct", "Pick / O-U %"), ("btotal_flag", "Total bet?"),
             ("cons_line", "Model avg spread"), ("spread_edge", "Avg spread edge"), ("lean", "Avg spread lean"),
             ("torvik_edge_pts", "Torvik edge"), ("torvik_lean", "Torvik lean"), ("torvik_watch", "Torvik watch (test)"),
             ("cons_total", "Model avg total"), ("total_edge_pts", "Avg total edge"), ("total_lean", "Avg total lean"),
             ("model_spread_sd", "Model disagreement (SD)"), ("n_models", "# models"),
             ("open_line", "Open spread"), ("open_total", "Open total")]
            + [(f"{s}_line", names[s] + tag(s, "spread")) for s in sites]
            + [(f"{s}_total", f"{names[s]} total" + tag(s, "total")) for s in sites])


RAW_COLS = ["away", "home", "cons_line", "cons_total", "model_spread_sd", "mkt_line", "mkt_total",
            "open_line", "open_total", "line_edge", "total_edge", "torvik_edge",
            "blend_margin", "bspread_side", "bspread_prob", "bspread_flag",
            "blend_total", "btotal_side", "btotal_prob", "btotal_flag"]


def save_raw(rows: list[dict], sites: list[str], day: dt.date) -> None:
    """Numeric copy of the sheet (home spreads) so the results step can grade it."""
    cols = RAW_COLS + [f"{s}_line" for s in sites] + [f"{s}_total" for s in sites]
    def wr(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    safe_write(OUT / f"raw_{day:%Y%m%d}.csv", wr)


ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/summary?event={eid}"


def _num(x):
    try:
        return float(str(x).lstrip("ou").replace("+", ""))
    except (TypeError, ValueError):
        return None


def espn_finals(day: dt.date, tm: TeamMatcher, want: set | None = None) -> dict[frozenset, dict]:
    """Final scores from the scoreboard feed, plus DraftKings closing lines from each
    game's summary feed (the scoreboard drops odds once a game is final).
    `want`: only fetch summaries for these team pairs (saves requests)."""
    import requests, time as _t
    hdr = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(ESPN_URL.format(ymd=f"{day:%Y%m%d}"), timeout=30, headers=hdr)
    r.raise_for_status()
    out = {}
    for ev in r.json().get("events", []):
        if not ev.get("status", {}).get("type", {}).get("completed"):
            continue
        comp = ev["competitions"][0]
        sides = {c["homeAway"]: c for c in comp["competitors"]}
        home = _team(sides["home"]["team"].get("displayName", ""), tm)
        away = _team(sides["away"]["team"].get("displayName", ""), tm)
        if not home or not away:
            continue
        key = frozenset((home, away))
        g = {"home": home, "home_score": _num(sides["home"].get("score")),
             "away_score": _num(sides["away"].get("score")), "close_line": None, "close_total": None}
        if want is None or key in want:
            try:
                sm = requests.get(ESPN_SUMMARY.format(eid=ev["id"]), timeout=30, headers=hdr).json()
                pc = (sm.get("pickcenter") or [{}])[0]
                ps = pc.get("pointSpread", {}).get("home", {}).get("close", {})
                tt = pc.get("total", {}).get("over", {}).get("close", {})
                g["close_line"] = _num(ps.get("line")) if ps.get("line") else _num(pc.get("spread"))
                g["close_total"] = _num(tt.get("line")) if tt.get("line") else _num(pc.get("overUnder"))
                _t.sleep(0.2)
            except Exception:
                pass
        out[key] = g
    return out


LOG_COLS = ["date", "away", "home", "home_score", "away_score", "mkt_line", "close_line", "cons_line", "spread_edge",
            "spread_bet", "spread_result", "spread_clv", "mkt_total", "close_total", "cons_total", "total_edge",
            "total_bet", "total_result", "total_clv", "model_spread_sd"]


def grade_day(day: dt.date, tm: TeamMatcher, sites: list[str]) -> int:
    """Grade the sheet for `day` and append it to output/results_log.csv. Returns games graded."""
    raw = OUT / f"raw_{day:%Y%m%d}.csv"
    log = OUT / "results_log.csv"
    src = log_file()
    if not raw.exists():
        return 0
    done = set()
    if src.exists():
        with open(src, newline="", encoding="utf-8") as f:
            done = {(r["date"], r["away"], r["home"]) for r in csv.DictReader(f)}
    with open(raw, newline="", encoding="utf-8") as f:
        want = {frozenset((r["away"], r["home"])) for r in csv.DictReader(f) if r["mkt_line"]}
    finals = espn_finals(day, tm, want)
    model_cols = [c for s in sites for c in (f"{s}_line", f"{s}_total")]
    new = []
    with open(raw, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if (day.isoformat(), r["away"], r["home"]) in done or not r["mkt_line"]:
                continue
            fin = finals.get(frozenset((r["away"], r["home"])))
            if not fin or fin["home_score"] is None:
                continue
            flip = fin["home"] != r["home"]        # ESPN listed the teams the other way round
            hs, as_ = (fin["away_score"], fin["home_score"]) if flip else (fin["home_score"], fin["away_score"])
            cl = fin["close_line"]
            if cl is not None and flip:
                cl = -cl
            margin, total = hs - as_, hs + as_
            g = {"date": day.isoformat(), "away": r["away"], "home": r["home"], "home_score": hs, "away_score": as_,
                 "mkt_line": r["mkt_line"], "close_line": "" if cl is None else cl, "cons_line": r["cons_line"],
                 "mkt_total": r["mkt_total"], "close_total": "" if fin["close_total"] is None else fin["close_total"],
                 "cons_total": r["cons_total"], "model_spread_sd": r["model_spread_sd"],
                 "open_line": r.get("open_line", ""), "open_total": r.get("open_total", "")}
            line, edge = float(r["mkt_line"]), float(r["line_edge"] or 0)
            g["spread_edge"] = abs(edge)
            if edge:
                home_side = edge > 0
                g["spread_bet"] = "home" if home_side else "away"
                cover = margin + line                       # >0 home covers
                g["spread_result"] = "P" if cover == 0 else ("W" if (cover > 0) == home_side else "L")
                if cl is not None:
                    g["spread_clv"] = round((line - cl) if home_side else (cl - line), 1)
            if r["mkt_total"] and r["total_edge"]:
                tline, tedge = float(r["mkt_total"]), float(r["total_edge"])
                g["total_edge"] = abs(tedge)
                if tedge:
                    over = tedge > 0
                    g["total_bet"] = "over" if over else "under"
                    g["total_result"] = "P" if total == tline else ("W" if (total > tline) == over else "L")
                    if fin["close_total"] is not None:
                        g["total_clv"] = round((fin["close_total"] - tline) if over else (tline - fin["close_total"]), 1)
            # The blended model's own picks (sheets from before it existed have none)
            if r.get("bspread_side"):
                hs_side = r["bspread_side"] == "home"
                cover = margin + line
                g.update(bspread_side=r["bspread_side"], bspread_prob=r.get("bspread_prob", ""),
                         bspread_flag=r.get("bspread_flag", ""),
                         bspread_result="P" if cover == 0 else ("W" if (cover > 0) == hs_side else "L"))
                if cl is not None:
                    g["bspread_clv"] = round((line - cl) if hs_side else (cl - line), 1)
            if r.get("btotal_side") and r["mkt_total"]:
                tline, over = float(r["mkt_total"]), r["btotal_side"] == "over"
                g.update(btotal_side=r["btotal_side"], btotal_prob=r.get("btotal_prob", ""),
                         btotal_flag=r.get("btotal_flag", ""),
                         btotal_result="P" if total == tline else ("W" if (total > tline) == over else "L"))
                if fin["close_total"] is not None:
                    g["btotal_clv"] = round((fin["close_total"] - tline) if over else (tline - fin["close_total"]), 1)
            g["torvik_edge"] = r.get("torvik_edge", "")
            for c in model_cols:           # each model's own number, for accuracy tracking
                g[c] = r.get(c, "")
            new.append(g)
    if new:
        old, cols = [], LOG_COLS + BLEND_LOG_COLS + model_cols
        if src.exists():
            with open(src, newline="", encoding="utf-8") as f:
                rd = csv.DictReader(f)
                old = list(rd)
                cols = list(rd.fieldnames) + [c for c in cols if c not in rd.fieldnames]
        def wr(path):
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                w.writerows(old + new)
        try:
            write_log_atomic(log, wr)
        except PermissionError:
            print("  WARNING: results_log.csv is open in Excel - grades saved to results_log.tmp instead.\n"
                  "           Close the log in Excel; the next run will merge it back automatically.")
            return len(new)
    return len(new)


BLEND_LOG_COLS = ["open_line", "open_total", "torvik_edge", "bspread_side", "bspread_prob", "bspread_flag", "bspread_result", "bspread_clv",
                  "btotal_side", "btotal_prob", "btotal_flag", "btotal_result", "btotal_clv"]


def log_file() -> Path:
    """The results log to READ. If an earlier run couldn't swap in its update
    (results_log.csv was open in Excel), the newer results_log.tmp is used, so
    nothing graded is lost and later steps see the latest grades."""
    log, tmp = OUT / "results_log.csv", OUT / "results_log.tmp"
    if tmp.exists() and (not log.exists() or tmp.stat().st_mtime > log.stat().st_mtime):
        try:
            os.replace(tmp, log)          # swap it in now if the lock is gone
        except OSError:
            return tmp
    return log


def write_log_atomic(log: Path, wr) -> None:
    """Write to a temp file, then swap it in, so a crash can never leave a half-written log."""
    tmp = log.with_suffix(".tmp")
    wr(tmp)
    os.replace(tmp, log)


def backfill_clv(tm: TeamMatcher) -> int:
    """Fill closing lines / CLV for logged games that were graded before closing
    lines were available. Rewrites results_log.csv in place."""
    log = OUT / "results_log.csv"
    src = log_file()
    if not src.exists():
        return 0
    with open(src, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        cols, rows = list(rd.fieldnames), list(rd)
    cols += [c for c in BLEND_LOG_COLS if c not in cols]
    todo = sorted({r["date"] for r in rows if not r.get("close_line")})
    n = 0
    for d in todo:
        want = {frozenset((r["away"], r["home"])) for r in rows if r["date"] == d and not r.get("close_line")}
        try:
            fin = espn_finals(dt.date.fromisoformat(d), tm, want)
        except Exception:
            continue
        for r in rows:
            if r["date"] != d or r.get("close_line"):
                continue
            g = fin.get(frozenset((r["away"], r["home"])))
            if not g:
                continue
            flip = g["home"] != r["home"]
            cl, ct = g["close_line"], g["close_total"]
            if cl is not None:
                cl = -cl if flip else cl
                r["close_line"] = cl
                if r.get("spread_bet"):
                    line = float(r["mkt_line"])
                    r["spread_clv"] = round((line - cl) if r["spread_bet"] == "home" else (cl - line), 1)
                n += 1
                if r.get("bspread_side"):
                    line = float(r["mkt_line"])
                    r["bspread_clv"] = round((line - cl) if r["bspread_side"] == "home" else (cl - line), 1)
            if ct is not None:
                r["close_total"] = ct
                if r.get("btotal_side") and r.get("mkt_total"):
                    t = float(r["mkt_total"])
                    r["btotal_clv"] = round((ct - t) if r["btotal_side"] == "over" else (t - ct), 1)
                if r.get("total_bet") and r.get("mkt_total"):
                    t = float(r["mkt_total"])
                    r["total_clv"] = round((ct - t) if r["total_bet"] == "over" else (t - ct), 1)
    if n:
        def wr(path):
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                w.writerows(rows)
        try:
            write_log_atomic(log, wr)
        except PermissionError:
            print("  NOTE: results_log.csv is open in another program - closing lines not saved this run.")
            return 0
    return n


def backfill_open(tm: TeamMatcher | None = None) -> int:
    """Copy opening lines from each day's raw_<date>.csv into results_log rows that
    were graded before the log kept them."""
    log = OUT / "results_log.csv"
    src = log_file()
    if not src.exists():
        return 0
    with open(src, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        cols, rows = list(rd.fieldnames), list(rd)
    for c in ("open_line", "open_total"):
        if c not in cols:
            cols.append(c)
    n, cache = 0, {}
    for r in rows:
        if r.get("open_line"):
            continue
        d = r["date"]
        if d not in cache:
            raw = OUT / f"raw_{d.replace('-', '')}.csv"
            cache[d] = {}
            if raw.exists():
                with open(raw, newline="", encoding="utf-8") as f:
                    cache[d] = {(x["away"], x["home"]): x for x in csv.DictReader(f)}
        x = cache[d].get((r["away"], r["home"]))
        if x and x.get("open_line"):
            r["open_line"], r["open_total"] = x["open_line"], x.get("open_total", "")
            n += 1
    if n:
        def wr(path):
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
        try:
            write_log_atomic(log, wr)
        except PermissionError:
            return 0
    return n


def results_summary(sites: list[str]) -> str:
    log = OUT / "results_log.csv"
    src = log_file()
    if not src.exists():
        return ""
    with open(src, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return ""
    def rec(rs, kind):
        w = sum(r[f"{kind}_result"] == "W" for r in rs); l = sum(r[f"{kind}_result"] == "L" for r in rs)
        p = sum(r[f"{kind}_result"] == "P" for r in rs)
        clv = [float(r[f"{kind}_clv"]) for r in rs if r.get(f"{kind}_clv")]
        pct = f"{100 * w / (w + l):.0f}%" if w + l else "-"
        return f"{w}-{l}-{p} ({pct})" + (f", avg CLV {statistics.mean(clv):+.1f}" if clv else "")
    out = [f"Results log: {len(rows)} games graded ({rows[0]['date']} to {rows[-1]['date']})",
           "  Need 52.4% to profit at -110. CLV = points the line moved your way before tip-off.", "",
           "  Edge size (pts)".ljust(24) + "Spread record".ljust(36) + "Total record"]
    for lo, hi, t_lo, t_hi in [(0, 2, 0, 3), (2, 4, 3, 5), (4, 6, 5, 8), (6, 99, 8, 999)]:
        sp = [r for r in rows if r.get("spread_result") and lo <= float(r["spread_edge"] or 0) < hi]
        to = [r for r in rows if r.get("total_result") and t_lo <= float(r["total_edge"] or 0) < t_hi]
        out.append(f"  sp {lo}-{hi if hi < 99 else '+'} / tot {t_lo}-{t_hi if t_hi < 999 else '+'}".ljust(24)
                   + rec(sp, "spread").ljust(36) + rec(to, "total"))
    agree = [r for r in rows if r.get("spread_result") and r["model_spread_sd"] and float(r["model_spread_sd"]) <= 3
             and float(r["spread_edge"] or 0) >= 3]
    out.append(f"  Spread edge 3+ with models agreeing (SD<=3): {rec(agree, 'spread')}")
    out += ["", "  Betting record of each model's own side when it differs from the market by 3+ pts (W-L, avg CLV):"]
    for s in sites:
        sp, to = model_record(rows, s, "spread"), model_record(rows, s, "total")
        if sp != "-" or to != "-":
            out.append(f"    {SITES[s]['name']:<14} spread {sp:<26} total {to}")
    out += ["", "  Background - average miss vs final margin / total (lower = more accurate, not the same as winning bets):"]
    for s in sites:
        errs = [abs(-float(r[f"{s}_line"]) - (float(r["home_score"]) - float(r["away_score"])))
                for r in rows if r.get(f"{s}_line")]
        terrs = [abs(float(r[f"{s}_total"]) - (float(r["home_score"]) + float(r["away_score"])))
                 for r in rows if r.get(f"{s}_total")]
        if errs:
            out.append(f"    {SITES[s]['name']:<14} margin {statistics.mean(errs):5.1f}  total "
                       f"{statistics.mean(terrs):5.1f}  ({len(errs)} games)" if terrs else "")
    mk = [abs(-float(r["close_line"]) - (float(r["home_score"]) - float(r["away_score"]))) for r in rows if r.get("close_line")]
    if mk:
        out.append(f"    {'Market close':<14} margin {statistics.mean(mk):5.1f}  ({len(mk)} games)")
    # Opening line: do the models beat the opener even if not the morning line?
    opened = [r for r in rows if r.get("open_line")]
    if len(opened) >= 30:
        out += ["", f"  Same test against the OPENING line ({len(opened)} games) - if these beat 52.4% but the"
                    " morning-line records don't, the edge is in betting earlier:"]
        for s in sites:
            sp, to = model_record(opened, s, "spread", vs="open"), model_record(opened, s, "total", vs="open")
            if sp != "-" or to != "-":
                out.append(f"    {SITES[s]['name']:<14} spread {sp.split(' CLV')[0]:<16} total {to.split(' CLV')[0]}")
    # Torvik watch: forward test only (games on/after WATCH_START)
    tw = [r for r in rows if r["date"] >= WATCH_START and r.get("open_line") and r.get("torvik_line") and r.get("mkt_line")]
    if tw:
        wo = lo_ = wm = lm = 0
        for r in tw:
            ol, ml, tl = float(r["open_line"]), float(r["mkt_line"]), float(r["torvik_line"])
            gap = ol - tl
            if abs(ml) >= WATCH_MAX_LINE or not (WATCH_GAP[0] <= abs(gap) < WATCH_GAP[1]):
                continue
            hm = float(r["home_score"]) - float(r["away_score"])
            for line, key in ((ol, "o"), (ml, "m")):
                res = hm + line
                if res == 0:
                    continue
                win = (res > 0) == (gap > 0)
                if key == "o":
                    wo += win; lo_ += not win
                else:
                    wm += win; lm += not win
        if wo + lo_:
            out += ["", f"  Torvik watch (forward test since {WATCH_START}): vs opening line {wo}-{lo_}"
                        f" ({100 * wo / (wo + lo_):.0f}%), vs morning line {wm}-{lm}"
                        + (f" ({100 * wm / (wm + lm):.0f}%)" if wm + lm else "")
                        + "  - needs ~150 bets before it means much"]
    # Trend: last 7 days vs the whole season
    cutoff = (dt.date.fromisoformat(rows[-1]["date"]) - dt.timedelta(days=6)).isoformat()
    recent = [r for r in rows if r["date"] >= cutoff]
    if len(recent) < len(rows):
        out += ["", f"  Trend - last 7 days ({len(recent)} games) vs season ({len(rows)}), own side at 3+ pts / avg miss:"]
        for s in sites:
            sp_all, sp_rec = model_record(rows, s, "spread"), model_record(recent, s, "spread")
            if sp_all == "-":
                continue
            def miss(rs):
                e = [abs(-float(r[f"{s}_line"]) - (float(r["home_score"]) - float(r["away_score"])))
                     for r in rs if r.get(f"{s}_line")]
                return f"{statistics.mean(e):.1f}" if e else "-"
            out.append(f"    {SITES[s]['name']:<14} spread {sp_rec:<26} (season {sp_all.split(' CLV')[0]})  miss {miss(recent)} (season {miss(rows)})")
        mk_r = [abs(-float(r["mkt_line"]) - (float(r["home_score"]) - float(r["away_score"]))) for r in recent]
        out.append(f"    {'Market line':<14} miss {statistics.mean(mk_r):.1f}")
        out.append("    (3-day swings in W-L are mostly luck; the avg-miss trend and CLV are the steadier signals)")
    bl = [r for r in rows if r.get("bspread_result")]
    if bl:
        out += ["", "  Blended model picks:"]
        for lab, kind in (("spread", "bspread"), ("total", "btotal")):
            allp = [r for r in rows if r.get(f"{kind}_result")]
            bets = [r for r in allp if r.get(f"{kind}_flag") in ("BET", "lean")]
            out.append(f"    {lab:<7} all picks {pick_rec(allp, kind):<30} flagged BET/lean {pick_rec(bets, kind)}")
    W = load_weights()
    if W.get("n"):
        import math
        def wtxt(kind):
            Wk = W.get(kind, {})
            ws = Wk.get("weights", {})
            if not ws:
                hit = Wk.get("cv_strong_hit")
                return ("no model beats a coin flip out-of-sample yet (market only)"
                        + (f"; best test hit rate {hit:.0%}" if hit else ""))
            cap = Wk.get("cap") or 99
            parts = [f"{SITES[k]['name']}: 3-pt gap -> {1 / (1 + math.exp(-v * min(3, cap))):.1%} cover" for k, v in ws.items()]
            return "; ".join(parts) + (f" (gaps capped at {cap} pts)" if cap < 99 else "")
        out += ["", f"  Calibration (from {W['n']} graded games, {W['updated']}), fitted on bet results:",
                f"    spread: {wtxt('spread')}", f"    total:  {wtxt('total')}"]
    return "\n".join(out)


def pick_rec(rs, kind):
    w = sum(r[f"{kind}_result"] == "W" for r in rs); l = sum(r[f"{kind}_result"] == "L" for r in rs)
    clv = [float(r[f"{kind}_clv"]) for r in rs if r.get(f"{kind}_clv")]
    pct = f"{100 * w / (w + l):.0f}%" if w + l else "-"
    return f"{w}-{l} ({pct})" + (f" CLV {statistics.mean(clv):+.2f}" if clv else "")


def model_record(rows, s, kind, min_diff=3.0, vs="mkt"):
    """W-L and avg CLV of model `s`'s own side against the morning market
    (vs="mkt") or the opening line (vs="open")."""
    lk, tk = ("mkt_line", "mkt_total") if vs == "mkt" else ("open_line", "open_total")
    w = l = 0
    clvs = []
    for r in rows:
        try:
            hm = float(r["home_score"]) - float(r["away_score"]); tot = float(r["home_score"]) + float(r["away_score"])
            if kind == "spread":
                mline, pred = float(r[lk]), -float(r[f"{s}_line"])
                diff = pred + mline                       # >0: model likes home more than market
                res = hm + mline
                close = float(r["close_line"]) if r.get("close_line") else None
                clv = None if close is None else ((mline - close) if diff > 0 else (close - mline))
            else:
                mline, pred = float(r[tk]), float(r[f"{s}_total"])
                diff = pred - mline
                res = tot - mline
                close = float(r["close_total"]) if r.get("close_total") else None
                clv = None if close is None else ((close - mline) if diff > 0 else (mline - close))
        except (KeyError, ValueError, TypeError):
            continue
        if abs(diff) < min_diff or res == 0:
            continue
        if (res > 0) == (diff > 0):
            w += 1
        else:
            l += 1
        if clv is not None:
            clvs.append(clv)
    if not w + l:
        return "-"
    return f"{w}-{l} ({100 * w / (w + l):.0f}%)" + (f" CLV {statistics.mean(clvs):+.2f}" if clvs else "")


def write_outputs(rows: list[dict], sites: list[str], day: dt.date) -> tuple[Path, Path]:
    OUT.mkdir(exist_ok=True)
    def biggest_edge(r):
        return max([abs(r[k]) for k in ("line_edge", "total_edge") if r[k] != ""], default=-1)
    far = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    # By tip-off time when market data gives one, then biggest edge first.
    rows.sort(key=lambda r: (r["_start"] or far, -biggest_edge(r), -(r["model_spread_sd"] or 0)))
    save_raw(rows, sites, day)

    def fav(r, v):   # "Navy -8.5" instead of a home spread
        if v == "" or v is None:
            return ""
        v = round(float(v), 1)
        return "PK" if v == 0 else (f"{r['home']} {v:g}" if v < 0 else f"{r['away']} {-v:g}")
    for r in rows:
        r["spread_edge"] = abs(r["line_edge"]) if r["line_edge"] != "" else ""
        r["total_edge_pts"] = abs(r["total_edge"]) if r["total_edge"] != "" else ""
        for c in [f"{s}_line" for s in sites] + ["cons_line", "mkt_line", "open_line", "blend_line"]:
            r[c] = fav(r, r.get(c, ""))

    cols = out_columns(sites)
    csv_path = OUT / f"cbb_consensus_{day:%Y%m%d}.csv"
    def wr_csv(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([h for _, h in cols])
            w.writerows([r.get(k, "") for k, _ in cols] for r in rows)
    csv_path = safe_write(csv_path, wr_csv)

    head = "".join(f"<th>{html.escape(h)}</th>" for _, h in cols)
    body = []
    for r in rows:
        tds = []
        for k, _ in cols:
            v = r.get(k, "")
            cls = ""
            if k in ("bspread_flag", "btotal_flag") and v == "BET":
                cls = " class=bet"
            elif k in ("bspread_flag", "btotal_flag") and v == "lean":
                cls = " class=lean"
            elif k in ("bspread_flag", "btotal_flag") and v:
                cls = " class=skip"
            elif k in ("spread_edge", "total_edge_pts", "torvik_edge_pts") and v != "" and v >= (5 if k == "total_edge_pts" else 3):
                cls = " class=hot"
            tds.append(f"<td{cls}>{html.escape(str(v))}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    page = f"""<!doctype html><meta charset=utf-8><title>CBB consensus {day}</title>
<style>body{{font:13px system-ui,sans-serif;margin:16px}}table{{border-collapse:collapse}}
th,td{{padding:4px 8px;border-bottom:1px solid #ddd;text-align:right;white-space:nowrap}}
td:nth-child(-n+5),td:nth-child(6),td:nth-child(7),td:nth-child(10),td:nth-child(11),td:nth-child(14),td:nth-child(16),td:nth-child(19){{text-align:left}}
th{{position:sticky;top:0;background:#f4f4f4}}
td:nth-child(4),td:nth-child(8),td:nth-child(12),th:nth-child(4),th:nth-child(8),th:nth-child(12){{border-left:2px solid #999}}
.hot{{background:#ffe08a;font-weight:600}}.bet{{background:#b7e4c7;font-weight:700}}.lean{{background:#e9f5ec}}.skip{{color:#999}}</style>
<h2>College basketball model consensus - {day:%a %b} {day.day}, {day.year}</h2>
<p><b>Blended</b> = market line adjusted by each model's calibrated weight (see the summary in the terminal).
<b>Pick / %</b> = the side the blend favors and its estimated chance to cover. <b>BET</b> (green) = 54.5%+ and the
line hasn't moved 2.5+ against it since opening. Until 300 games are graded these show as "lean" (pale green) instead of BET.
Early in the season the weights are near zero, so expect few flags.
The model-average and Torvik columns are kept for comparison.</p>
<table><tr>{head}</tr>{''.join(body)}</table>"""
    html_path = OUT / f"cbb_consensus_{day:%Y%m%d}.html"
    html_path = safe_write(html_path, lambda p: p.write_text(page, encoding="utf-8"))
    return csv_path, html_path


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--sites", default=",".join(k for k, v in SITES.items() if v["enabled"]),
                    help="comma list from: " + ",".join(SITES))
    ap.add_argument("--dump", action="store_true", help="only download and save raw pages")
    ap.add_argument("--from-cache", action="store_true", help="parse saved pages instead of downloading")
    ap.add_argument("--min-models", type=int, default=2, help="hide games with fewer models (default 2)")
    ap.add_argument("--grade", nargs="?", const="yesterday", metavar="YYYY-MM-DD",
                    help="only grade a past day's sheet (default yesterday) and show the results summary")
    args = ap.parse_args(argv)

    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    all_sites = list(SITES)
    if args.grade or not (args.dump or args.from_cache):
        OUT.mkdir(exist_ok=True)
        tm0 = TeamMatcher(HERE / "team_aliases.csv")
        gday = dt.date.today() - dt.timedelta(days=1) if args.grade in (None, "yesterday") else dt.date.fromisoformat(args.grade)
        if args.grade is None:      # normal morning run: grade the last 3 days quietly
            gdays = [dt.date.today() - dt.timedelta(days=k) for k in (3, 2, 1)]
        else:
            gdays = [gday]
        n = 0
        for d in gdays:
            try:
                n += grade_day(d, tm0, all_sites)
            except Exception as e:
                print(f"  Grading {d} failed: {e}")
        if n:
            print(f"  Graded {n} finished games.")
        try:
            b = backfill_clv(tm0)
            if b:
                print(f"  Added closing lines for {b} earlier games.")
        except Exception as e:
            print(f"  Closing-line backfill failed: {e}")
        try:
            bo = backfill_open()
            if bo:
                print(f"  Added opening lines for {bo} earlier games.")
        except Exception as e:
            print(f"  Opening-line backfill failed: {e}")
        try:
            Wn = calibrate(all_sites)
            if Wn.get("n", 0) < 60:
                print(f"  Calibration: {Wn.get('n', 0)} graded games so far - using market-only until 60.")
        except Exception as e:
            print(f"  Calibration failed (using previous weights): {e}")
        summary = results_summary(all_sites)
        if args.grade:
            print(summary or "Nothing graded yet - run the script on a game day first.")
            return
        if summary:
            print(summary + "\n")
    sites = [s.strip() for s in args.sites.split(",") if s.strip()]
    CACHE.mkdir(exist_ok=True)
    tm = TeamMatcher(HERE / "team_aliases.csv")

    projs: list[Proj] = []
    for s in sites:
        cfg = SITES[s]
        if cfg.get("today_only") and day != dt.date.today():
            cached_only = cache_path(s, day)
            if not (args.from_cache and cached_only.exists()):
                print(f"  {cfg['name']:<14} skipped (only shows today's games)")
                continue
        url = cfg["url"].format(ymd=f"{day:%Y%m%d}", iso=day.isoformat())
        cached = cache_path(s, day)
        try:
            if args.from_cache:
                if not cached.exists():
                    print(f"  {cfg['name']:<14} no cached page"); continue
                page = cached.read_text(encoding="utf-8")
            else:
                page = fetch(s, url, cfg["wait_for"], day)
        except Exception as e:  # one site failing shouldn't kill the run
            print(f"  {cfg['name']:<14} FAILED to load: {e}")
            continue
        if args.dump:
            print(f"  {cfg['name']:<14} saved {cached.name} ({len(page)//1024} KB)"); continue
        got = parse_site(s, page, tm, day)
        bad = [p for p in got if len(set(p.teams)) < 2]
        for p in bad:   # both names matched the same team - an alias problem; drop it, don't crash
            print(f"  WARNING {cfg['name']}: skipped a game where both teams read as {p.teams[0]!r}: {p.raw[:120]!r}")
        got = [p for p in got if len(set(p.teams)) == 2]
        if s == "sasser" and not got and re.search(r"no game projections", page, re.I):
            print(f"  {cfg['name']:<14} not posted yet (usually around midday CT) - re-run this afternoon to add it")
            continue
        print(f"  {cfg['name']:<14} {len(got):>4} games")
        projs.extend(got)
    if args.dump:
        return

    market = {}
    try:
        market = market_lines(day, tm)
        if market:
            print(f"  {'Market (ESPN/DK)':<14} {len(market):>4} games")
    except Exception as e:
        print(f"  Market lines failed: {e}")

    games = combine(projs, market)
    kept = fill_from_earlier(games, day)
    if kept:
        print(f"  Kept {kept} lines from today's earlier run that are no longer posted (games started/finished)")
    W = load_weights()
    rows = [summarize(g, sites, W) for g in games
            if sum(SITES[x].get('in_consensus', True) for x in g.margins) >= args.min_models]
    if not rows:
        print("No games with enough model projections. Run with --dump and check the cache/ pages.")
        return
    csv_path, html_path = write_outputs(rows, sites, day)
    print(f"\n{len(rows)} games -> {csv_path.name}, {html_path.name}\n")
    if not W["spread"].get("weights") and not W["total"].get("weights"):
        print("Blended model = market only right now (no model's disagreement with the market has predicted bet\n"
              "results better than a coin flip in testing yet), so there are no picks.\n"
              "The model-average and Torvik columns in the sheet are still there for tracking.\n")
    print(f"{'Start':<9}{'Away':<18}{'Home':<18}{'Spread pick':<30}{'Flag':<8}{'Total pick':<22}{'Flag':<6}")
    for r in rows[:30]:
        print(f"{r['start']:<9}{r['away'][:17]:<18}{r['home'][:17]:<18}{r.get('cover_pct', '')[:29]:<30}"
              f"{(r.get('bspread_flag') or '')[:7]:<8}{r.get('ou_pct', '')[:21]:<22}{(r.get('btotal_flag') or '')[:6]}")


if __name__ == "__main__":
    main()
