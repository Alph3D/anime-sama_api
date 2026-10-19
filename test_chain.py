# test_chain.py
import asyncio, re, traceback, httpx
from anime_sama_api import find_site_url
from anime_sama_api.catalogue import Catalogue

QUERY = "detective conan"
EPISODE = 1
LANGS = ["VF", "VOSTFR"]

async def search_slugs(client, site, query):
    r = await client.get(f"{site}catalogue/", params={"search": query})
    r.raise_for_status()
    slugs = re.findall(r'href="(?:https?://[^"/]+)?/?catalogue/([^"/?#]+)/?"', r.text)
    return list(dict.fromkeys(slugs))

async def main():
    site = await find_site_url()
    async with httpx.AsyncClient(follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0"}) as client:
        slugs = await search_slugs(client, site, QUERY)
        print("1) slugs:", slugs)

        wanted = QUERY.lower().replace(" ", "-")
        slug = wanted if wanted in slugs else slugs[0]
        cat = Catalogue(url=f"{site}catalogue/{slug}/", name=slug, client=client)
        print("2) catalogue:", cat)

        try:
            seasons = await cat.seasons()
            print("3) saisons:", seasons)
            if not seasons:
                print("   ❌ aucune saison trouvée (regex panneauAnime obsolète ?)")
                return

            eps = await seasons[0].episodes()
            print("4) nb épisodes saison 1:", len(eps))
            if not eps:
                print("   ❌ aucun épisode (episodes.js / regex obsolète ?)")
                return

            ep = eps[EPISODE - 1]
            print("5) épisode:", ep.name, "| langues:", list(ep.languages.availables.keys()))
            print("6) lecteurs:", list(ep.consume_player(LANGS)))
        except Exception:
            traceback.print_exc()

asyncio.run(main())