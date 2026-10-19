import asyncio, re, httpx
from anime_sama_api import find_site_url

async def main():
    site = await find_site_url()
    print("site:", repr(site))
    async with httpx.AsyncClient(follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0"}) as c:
        r = await c.get(f"{site}catalogue/?search=conan")
        print("status:", r.status_code, "| url finale:", r.url, "| taille:", len(r.text))
        links = re.findall(r'href="([^"]*catalogue/[^"]+)"', r.text)
        print("liens catalogue:", len(links), links[:5])
        t = r.text.lower()
        print("anti-bot ?", "just a moment" in t or "cloudflare" in t)
        open("page.html", "w", encoding="utf-8").write(r.text)

asyncio.run(main())