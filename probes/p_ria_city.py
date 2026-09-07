import time, json, httpx, collections
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
c=httpx.Client(headers={"User-Agent":UA,"Accept":"application/json"},timeout=25,follow_redirects=True)
r=c.get("https://dom.ria.com/node/searchEngine/v2/",params={"category":1,"realty_type":2,"operation_type":1,"state_id":15,"page":0,"limit":20}).json()
seen=collections.Counter(); sample=None
for rid in r["items"][:12]:
    d=c.get(f"https://dom.ria.com/realty/data/{rid}?lang_id=4").json()
    seen[(d.get("city_id"),d.get("city_name_uk"))]+=1
    if d.get("city_name_uk")=="Івано-Франківськ" and sample is None: sample=d
    time.sleep(0.35)
print("CITY IDS:", seen.most_common())
if sample:
    print("\n--- FIELDS OF ONE IVANO-FRANKIVSK CARD ---")
    for k in sorted(sample):
        v=sample[k]
        if isinstance(v,(str,int,float)) and str(v).strip() and len(str(v))<80: print(f"  {k} = {v}")
    json.dump(sample,open("probes/_ria_sample.json","w"),ensure_ascii=False,indent=1)
