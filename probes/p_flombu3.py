import json, httpx
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
c=httpx.Client(headers={"User-Agent":UA,"Accept":"application/json"},timeout=30,follow_redirects=True)
BB={"south":48.86,"west":24.62,"north":48.98,"east":24.80}
variants={
 "plain":            {**BB,"estate_type":"flat"},
 "filter[]":         {f"filter[{k}]":v for k,v in BB.items()} | {"filter[estate_type]":"flat"},
 "q[]":              {f"q[{k}]":v for k,v in BB.items()},
 "plain+label":      {**BB,"bounds_label":"Івано-Франківськ","bounds_country":"ua","estate_type":"flat","deal_type":"estate_deal_sale"},
}
for name,p in variants.items():
    try:
        r=c.get("https://flombu.com/uk/estate_deal_sales.json",params=p).json()
        loc={i["attributes"].get("adminArea1","?") for i in r.get("included",[])}
        print(f"{name:<12} pages={r['meta'].get('pages'):<5} n={len(r['data'])} areas={sorted(loc)[:3]}")
    except Exception as e: print(f"{name:<12} ERR {e}")
