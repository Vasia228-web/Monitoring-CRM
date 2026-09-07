import json, httpx
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
c=httpx.Client(headers={"User-Agent":UA,"Accept":"application/json"},timeout=30,follow_redirects=True)
r=c.get("https://flombu.com/uk/estate_deal_sales.json").json()
print("top:", list(r)); print("count:", len(r.get("data",[])))
print("meta:", json.dumps(r.get("meta"),ensure_ascii=False)[:300])
print("links:", json.dumps(r.get("links"),ensure_ascii=False)[:400])
a=r["data"][0]["attributes"]
print("\n--- ATTRIBUTES ---")
for k,v in sorted(a.items()):
    print(f"  {k} = {json.dumps(v,ensure_ascii=False)[:110]}")
json.dump(r["data"][0],open("probes/_flombu_sample.json","w"),ensure_ascii=False,indent=1)
