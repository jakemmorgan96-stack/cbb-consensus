"""Fetch today's Torvik page on its own (used by the parallel 'torvik' jobs on GitHub).

Torvik's site blocks some GitHub server addresses. Each parallel job runs on a
different machine, so several tries at once usually get at least one through.
Exits 1 if this machine got blocked or the page had no games."""
import datetime as dt, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cbb_consensus as c

day = dt.date.today()
cfg = c.SITES["torvik"]
c.CACHE.mkdir(exist_ok=True)
if (c.HERE / "manual" / f"torvik_{day:%Y%m%d}.csv").exists():
    sys.exit("Torvik typed in by hand for today - skipping download")  # exits 1: nothing to upload
try:
    page = c.fetch("torvik", cfg["url"].format(ymd=f"{day:%Y%m%d}", iso=day.isoformat()), cfg["wait_for"], day)
except Exception as e:
    sys.exit(f"Torvik failed to load: {e}")
tm = c.TeamMatcher(c.HERE / "team_aliases.csv")
n = len(c.parse_site("torvik", page, tm, day))
print(f"Torvik: {n} games")
if not n:
    sys.exit(1)
