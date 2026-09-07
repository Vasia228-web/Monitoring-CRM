import asyncio, re
from playwright.async_api import async_playwright
URL="https://www.olx.ua/uk/nedvizhimost/kvartiry/prodazha-kvartir/ivano-frankovsk/"
async def main():
    async with async_playwright() as pw:
        b=await pw.chromium.launch(headless=True)
        ctx=await b.new_context(locale="uk-UA", timezone_id="Europe/Kyiv",
              viewport={"width":1366,"height":900})
        p=await ctx.new_page()
        r=await p.goto(URL, wait_until="domcontentloaded", timeout=60000)
        print("status:", r.status if r else None)
        await p.wait_for_timeout(3500)
        html=await p.content()
        print("len:", len(html), "| title:", (await p.title())[:80])
        print("cards l-card:", html.count('data-cy="l-card"'), "| ad-card:", html.count("data-testid=\"ad-card"))
        print("blocked?", "403" in (await p.title()) or "Request blocked" in html)
        open("probes/_olx.html","w").write(html)
        await b.close()
asyncio.run(main())
