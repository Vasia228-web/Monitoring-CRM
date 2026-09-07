import json, httpx
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
c = httpx.Client(headers={"User-Agent": UA, "Accept": "application/json"}, timeout=25, follow_redirects=True)
d = c.get("https://dom.ria.com/realty/data/34606244?lang_id=4").json()
print("TOP KEYS:", len(d))
for k in sorted(d):
    v = d[k]
    if isinstance(v, (str, int, float)) and str(v)[:60].strip():
        print(f"  {k} = {str(v)[:70]}")
