"""Map DOM.RIA state_id -> oblast name, then find Ivano-Frankivsk city_id."""
import time, httpx
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
S = "https://dom.ria.com/node/searchEngine/v2/"
D = "https://dom.ria.com/realty/data/{}?lang_id=4"
c = httpx.Client(headers={"User-Agent": UA, "Accept": "application/json"}, timeout=25, follow_redirects=True)

for sid in range(1, 28):
    try:
        r = c.get(S, params={"category": 1, "realty_type": 2, "operation_type": 1,
                             "state_id": sid, "page": 0, "limit": 1}).json()
        if not r.get("items"):
            print(f"state_id={sid:<3} count={r.get('count')} (empty)"); continue
        d = c.get(D.format(r["items"][0])).json()
        name = d.get("state_name_uk")
        mark = "  <<< TARGET" if "ранків" in str(name) else ""
        print(f"state_id={sid:<3} count={r.get('count'):<6} {name}  city={d.get('city_name_uk')}{mark}")
    except Exception as e:
        print(f"state_id={sid:<3} ERR {type(e).__name__}: {e}")
    time.sleep(0.4)
