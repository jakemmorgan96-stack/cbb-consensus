import requests, platform
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
u = "https://barttorvik.com/schedule.php?date=20261118&conlimit="
try:
    r = requests.get(u, headers=H, timeout=20)
    print("RESULT", platform.system(), r.status_code, len(r.content), "tables" if "<td" in r.text else "no-table", repr(r.text[:60]))
except Exception as e:
    print("RESULT", platform.system(), "ERR", e)
print("IP", requests.get("https://api.ipify.org", timeout=10).text)
