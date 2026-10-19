import asyncio
from anime_sama_api import AnimeSama, find_site_url

async def main():
    site = await find_site_url()
    print("site:", site)
    api = AnimeSama(site)
    res = await api.search("conan")   # essaie aussi "detective conan"
    print("type:", type(res), "| nb:", len(res) if hasattr(res, "__len__") else "?")
    for r in res:
        print("->", r)
        print("   attributs:", [a for a in dir(r) if not a.startswith("_")])

asyncio.run(main())