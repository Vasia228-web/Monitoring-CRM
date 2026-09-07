import httpx
H={"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
   "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
   "Accept-Language":"uk-UA,uk;q=0.9,en;q=0.8","Accept-Encoding":"gzip, deflate, br",
   "Sec-Fetch-Dest":"document","Sec-Fetch-Mode":"navigate","Sec-Fetch-Site":"none","Sec-Fetch-User":"?1",
   "Upgrade-Insecure-Requests":"1","Connection":"keep-alive"}
for url in ["https://www.olx.ua/uk/nedvizhimost/kvartiry/prodazha-kvartir/ivano-frankovsk/",
            "https://www.olx.ua/api/v1/offers/?limit=2&category_id=1147&city_id=5"]:
    for h2 in (True, False):
        try:
            with httpx.Client(headers=H,timeout=30,follow_redirects=True,http2=h2) as c:
                r=c.get(url)
                print(f"http2={h2} {r.status_code} len={len(r.content)} {url[:60]}")
        except Exception as e:
            print(f"http2={h2} ERR {type(e).__name__}: {str(e)[:80]}")
