import re, json
h=open("/tmp/lun_sale.html",encoding="utf-8",errors="ignore").read()
# collect Next.js flight payload chunks
chunks=re.findall(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)', h)
print("chunks:",len(chunks))
payload="".join(json.loads('"'+c+'"') for c in chunks)
print("payload len:",len(payload))
for probe in ('"rooms"','"totalArea"','"price"','"url"','"realty','"id":','"street'):
    print(f"  {probe}: {payload.count(probe)}")
# find a realty-looking object
m=re.search(r'\{"id":\d+,[^{}]{0,400}"price"', payload)
print("\nsample around price:", payload[m.start():m.start()+700] if m else "none")
open("probes/_lun_payload.txt","w").write(payload)
