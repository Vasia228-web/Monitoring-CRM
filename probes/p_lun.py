from bs4 import BeautifulSoup
h=open("/tmp/lun_sale.html",encoding="utf-8",errors="ignore").read()
s=BeautifulSoup(h,"lxml")
cards=s.select('[data-testid="realty-card-container"]')
print("cards:",len(cards))
c=cards[0]
print("\n--- CARD 0 TEXT ---")
print(c.get_text(" | ",strip=True)[:600])
print("\n--- CARD 0 LINKS ---")
for a in c.select("a[href]")[:6]: print("  ",a["href"][:110])
print("\n--- CARD 0 CLASS TREE (depth 3) ---")
for el in c.find_all(True, recursive=True, limit=25):
    cl=" ".join(el.get("class",[]))[:60]
    if cl: print(f"  <{el.name}> .{cl}")
