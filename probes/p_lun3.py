import re, json
p=open("probes/_lun_payload.txt",encoding="utf-8").read()
i=p.find('{"id":4721489034')
# balanced-brace scan
d=0
for j in range(i,len(p)):
    if p[j]=='{': d+=1
    elif p[j]=='}':
        d-=1
        if d==0: break
obj=json.loads(p[i:j+1])
print("KEYS:",len(obj))
for k in sorted(obj):
    v=obj[k]
    if v in (None,"",[],{}): continue
    print(f"  {k} = {json.dumps(v,ensure_ascii=False)[:95]}")
json.dump(obj,open("probes/_lun_sample.json","w"),ensure_ascii=False,indent=1)
