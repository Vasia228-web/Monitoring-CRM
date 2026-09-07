import json, httpx
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
c=httpx.Client(headers={"User-Agent":UA,"Accept":"application/json"},timeout=30,follow_redirects=True)
# Ivano-Frankivsk city bounding box
p={"south":48.86,"west":24.62,"north":48.98,"east":24.80,"bounds_label":"Івано-Франківськ","page":1}
r=c.get("https://flombu.com/uk/estate_deal_sales.json",params=p).json()
print("count:",len(r["data"]))
print("meta keys:",list(r.get("meta",{})))
for k in ("pagination","totalCount","total","pages"):
    if k in r.get("meta",{}): print(f"  meta.{k}:",json.dumps(r["meta"][k],ensure_ascii=False)[:200])
for d in r["data"][:6]:
    a=d["attributes"]
    print(f"  id={d['id']:<6} {a.get('addressToStreet','?')[:34]:<34} {a.get('priceHumanVal','?'):<14} {a.get('type2HumanVal')} {a.get('tileEstateAccentAttrs')}")
print("\nincluded types:", {i["type"] for i in r.get("included",[])})
json.dump(r,open("probes/_flombu_if.json","w"),ensure_ascii=False)
