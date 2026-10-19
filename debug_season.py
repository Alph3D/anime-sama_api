import asyncio, re, httpx
from anime_sama_api import find_site_url
from anime_sama_api.catalogue import Catalogue

async def main():
    site = await find_site_url()
    async with httpx.AsyncClient(follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0"}) as client:
        cat = Catalogue(url=f"{site}catalogue/detective-conan/", name="detective-conan", client=client)
        seasons = await cat.seasons()
        s = seasons[0]
        print("URL saison:", s.url)

        for lang in ["vostfr", "vf"]:
            url = s.url + lang + "/"
            r = await client.get(url)
            print(f"\n== {url} -> {r.status_code}, {len(r.text)} octets, url finale: {r.url}")
            scripts = re.findall(r'<script[^>]+src="([^"]+)"', r.text)
            print("scripts:", scripts)
            print("episodes.js :", re.findall(r'[^"\']*episodes[^"\']*', r.text)[:5])
            open(f"saison_{lang}.html", "w", encoding="utf-8").write(r.text)

asyncio.run(main())