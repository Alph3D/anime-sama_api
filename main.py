import asyncio
import re
import time
import unicodedata
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from anime_sama_api import AnimeSama, find_site_url
from anime_sama_api.catalogue import Catalogue

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
CACHE_TTL = 30 * 60

api: AnimeSama | None = None
client: httpx.AsyncClient | None = None
site_url: str = ""

# ───────────── CACHE ─────────────
_cache: dict[str, tuple[object, float]] = {}

def cache_get(key):
    item = _cache.get(key)
    if not item:
        return None
    value, expires = item
    if expires < time.time():
        _cache.pop(key, None)
        return None
    return value

def cache_set(key, value, ttl=CACHE_TTL):
    _cache[key] = (value, time.time() + ttl)
    if len(_cache) > 500:
        _cache.pop(min(_cache, key=lambda k: _cache[k][1]), None)

# ───────────── CLIENT HTTP ─────────────
async def add_referer(request: httpx.Request):
    # Cloudflare renvoie 403 sur episodes.js sans Referer
    url = str(request.url)
    if "episodes.js" in url:
        request.headers["Referer"] = url.split("episodes.js")[0]
    elif "Referer" not in request.headers:
        request.headers["Referer"] = site_url

@asynccontextmanager
async def lifespan(app: FastAPI):
    global api, client, site_url
    site_url = await find_site_url()
    print(f"🌐 Domaine Anime-Sama : {site_url}")
    client = httpx.AsyncClient(
        follow_redirects=True,
        headers=UA,
        timeout=30,
        event_hooks={"request": [add_referer]},
    )
    api = AnimeSama(site_url, client=client)
    yield
    await client.aclose()

app = FastAPI(title="Anime-Sama API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ───────────── RECHERCHE ─────────────
def slugify(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")

async def find_catalogue(query: str) -> Catalogue | None:
    r = await client.get(f"{site_url}catalogue/", params={"search": query})
    r.raise_for_status()
    slugs = list(dict.fromkeys(
        re.findall(r'href="(?:https?://[^"/]+)?/?catalogue/([^"/?#]+)/?"', r.text)
    ))
    if not slugs:
        return None
    wanted = slugify(query)
    slug = (
        wanted if wanted in slugs
        else next((s for s in slugs if wanted in s), slugs[0])
    )
    return Catalogue(url=f"{site_url}catalogue/{slug}/", name=slug, client=client)

# ───────────── SAISONS / ÉPISODES ─────────────
def season_start(name: str) -> int | None:
    m = re.search(r"Episodes?\s*(\d+)", name, re.IGNORECASE)
    return int(m.group(1)) if m else None

def pick_season(seasons, episode: int, saison_num: int | None):
    if saison_num is not None:
        nums = [s for s in seasons if re.search(rf"saison\s*{saison_num}\b", s.name, re.IGNORECASE)]
        if not nums:
            raise HTTPException(404, f"Saison {saison_num} introuvable : {[s.name for s in seasons]}")
        return nums[0], episode - 1  # numéro relatif à la saison

    candidates = [(season_start(s.name), s) for s in seasons]
    candidates = [(st, s) for st, s in candidates if st is not None and st <= episode]
    if not candidates:
        raise HTTPException(404, f"Aucune saison ne contient l'épisode {episode}")
    start, season = max(candidates, key=lambda c: c[0])
    return season, episode - start

async def get_episodes(season):
    key = f"eps|{season.url}"
    eps = cache_get(key)
    if eps is None:
        eps = await season.episodes()
        cache_set(key, eps)
    return eps

# ───────────── EXTRACTION DU FLUX ─────────────
def _extract_sync(embed_url: str) -> dict | None:
    if yt_dlp is None:
        return None
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 15}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(embed_url, download=False)
    url = info.get("url")
    if not url and info.get("formats"):
        url = info["formats"][-1].get("url")
    if not url:
        return None
    is_hls = ".m3u8" in url or "m3u8" in str(info.get("protocol", ""))
    return {"url": url, "type": "hls" if is_hls else "mp4", "headers": info.get("http_headers", {})}

async def extract_stream(embed_url: str):
    try:
        return await asyncio.wait_for(asyncio.to_thread(_extract_sync, embed_url), timeout=25)
    except Exception as e:
        print(f"    ⚠️ extraction impossible ({embed_url}) : {str(e)[:120]}")
        return None

# ───────────── ROUTES ─────────────
@app.get("/health")
async def health():
    return {"status": "ok", "api_ready": api is not None, "yt_dlp": yt_dlp is not None}

@app.get("/resolve")
async def resolve(
    title: str = Query(...),
    episode: int = Query(..., ge=1),
    langue: str = Query("vostfr"),
    saison: int | None = Query(None, description="Si fourni, 'episode' est relatif à cette saison"),
):
    if api is None:
        raise HTTPException(503, "API non initialisée")
    print(f"\n🎬 {title} | Ep {episode} | {langue} | saison={saison}")

    cache_key = f"res|{slugify(title)}|{episode}|{langue.lower()}|{saison}"
    if (cached := cache_get(cache_key)):
        return {**cached, "cached": True}

    cat = await find_catalogue(title)
    if not cat:
        raise HTTPException(404, f"'{title}' introuvable sur Anime-Sama")
    print(f"  ✅ Catalogue : {cat.url}")

    seasons = await cat.seasons()
    if not seasons:
        raise HTTPException(404, "Aucune saison trouvée")

    season, idx = pick_season(seasons, episode, saison)
    print(f"  📺 Saison : {season.name} (index {idx})")

    episodes = await get_episodes(season)
    if not (0 <= idx < len(episodes)):
        raise HTTPException(404, f"Épisode hors limites ({len(episodes)} épisodes dans '{season.name}')")
    ep = episodes[idx]

    wanted = langue.upper()
    prefer = list(dict.fromkeys([wanted, "VF", "VOSTFR"]))
    embeds = list(dict.fromkeys(ep.consume_player(prefer)))
    if not embeds:
        raise HTTPException(404, f"Aucun lecteur pour cet épisode (langues : {list(ep.languages.availables)})")
    print(f"  🎞️ {len(embeds)} lecteurs")

    for embed in embeds:
        print(f"    ▶ {embed}")
        stream = await extract_stream(embed)
        if stream:
            resp = {
                "success": True, "anime": cat.name, "anime_url": cat.url,
                "saison": season.name, "episode": episode, "episode_name": ep.name,
                "langue": wanted, "lecteur": embed,
                "stream_url": stream["url"], "stream_type": stream["type"],
                "stream_headers": stream["headers"], "embed_urls": embeds,
            }
            cache_set(cache_key, resp)
            return resp

    # Aucun flux direct : on renvoie quand même les pages de lecteurs (iframe)
    resp = {
        "success": True, "anime": cat.name, "anime_url": cat.url,
        "saison": season.name, "episode": episode, "episode_name": ep.name,
        "langue": wanted, "lecteur": embeds[0],
        "stream_url": None, "stream_type": None, "embed_urls": embeds,
    }
    cache_set(cache_key, resp, ttl=5 * 60)
    return resp

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000)