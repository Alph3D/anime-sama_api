# debug_episodes.py
import asyncio, re, httpx
from anime_sama_api import find_site_url

async def main():
    site = await find_site_url()
    page_url = f"{site}catalogue/detective-conan/saison1/vostfr/"
    ua = {"User-Agent": "Mozilla/5.0"}

    async with httpx.AsyncClient(follow_redirects=True, headers=ua) as client:
        r = await client.get(page_url)
        html = r.text

        # 1) comment episodes.js est référencé exactement
        for m in re.finditer(r'.{0,80}episodes\.js\?filever=\d+.{0,40}', html):
            print("RÉFÉRENCE:", m.group(0))

        m = re.search(r"episodes\.js\?filever=\d+", html)
        js_url = page_url + m.group(0)
        print("\nURL testée:", js_url)

        # 2) sans / avec Referer
        for label, headers in [("sans referer", {}), ("avec referer", {"Referer": page_url})]:
            rr = await client.get(js_url, headers=headers)
            print(f"\n[{label}] status={rr.status_code} taille={len(rr.text)}")
            print(rr.text[:500])

        # 3) la fonction qui liste les noms d'épisodes
        i = html.find("resetListe")
        print("\n--- autour de resetListe ---")
        print(html[max(0, i-200): i+600] if i != -1 else "resetListe introuvable")

asyncio.run(main())