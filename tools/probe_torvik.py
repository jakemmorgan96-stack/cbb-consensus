"""One-off check of which Torvik URLs/methods GitHub's servers can reach."""
import requests
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
urls = ["https://barttorvik.com/schedule.php?date=20261118&conlimit=",
        "https://www.barttorvik.com/schedule.php?date=20261118&conlimit=",
        "https://barttorvik.com/",
        "https://barttorvik.com/2026_super_sked.json",
        "https://barttorvik.com/trank.php?year=2026&json=1",
        "https://barttorvik.com/timemachine/team_results/20261117_team_results.json.gz"]
for u in urls:
    for name, h in (("bare", {}), ("browser", H)):
        try:
            r = requests.get(u, headers=h, timeout=20)
            print(f"{r.status_code} {len(r.content):>8} {name:<8} {u} :: {r.text[:80]!r}")
        except Exception as e:
            print(f"ERR {name} {u} {e}")
