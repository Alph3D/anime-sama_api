"""
API Anime-Sama (FastAPI)

Routes :
  GET /health
  GET /search?q=...                         -> liste de séries (title, slug, url, langs)
  GET /resolve?title=...&episode=..&langue=vf|vostfr[&saison=N]
        - sans `saison` : `episode` est ABSOLU (ex. Conan épisode 1000)
        - avec `saison` : `episode` est RELATIF à la saison (ex. saison 5, épisode 20)

Variables d'environnement facultatives :
  ANIME_SAMA_SITE   domaine à utiliser (ex. https://anime-sama.to/) au lieu de la détection auto
  PORT              port d'écoute si lancé via `python main.py`
"""
import asyncio
import os
import re
import time
import unicodedata
from contextlib import asynccontextmanager
from html import unescape
from urllib.parse import urlparse

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from anime_sama_api import AnimeSama, find_site_url
from anime_sama_api.catalogue import Catalogue

try:
    import yt_dlp
except ImportError:  # l'API démarre quand même, mais sans extraction de flux
    yt_dlp = None

# ───────────────────────── RÉGLAGES ─────────────────────────
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
CACHE_TTL = 30 * 60          # flux résolus
SEARCH_TTL = 10 * 60         # résultats de recherche
RESOLVE_BUDGET = 50.0        # secondes max pour trouver un flux
EXTRACT_TIMEOUT = 18.0       # secondes max par lecteur
PARALLEL_EXTRACT = 3         # lecteurs testés en parallèle (par langue)

# Hébergeurs essayés en premier (les autres suivent dans l'ordre du site)
PREFER_HOSTS = [
    "sendvid.com",
    "vidmoly.net",
    "vidmoly.to",
    "uqload.vc",
    "ansembed.net",
    "video.sibnet.ru",
]

STOP_TOKENS = {"season", "saison", "vf", "vostfr", "part", "cour", "the", "tv"}

api: AnimeSama | None = None
client: httpx.AsyncClient | None = None
site_url: str = ""


# ───────────────────────── CACHE ─────────────────────────
_cache: dict[str, tuple[object, float]] = {}


def cache_get(key: str):
    item = _cache.get(key)
    if not item:
        return None
    value, expires = item
    if expires < time.time():
        _cache.pop(key, None)
        return None
    return value


def cache_set(key: str, value, ttl: int = CACHE_TTL):
    _cache[key] = (value, time.time() + ttl)
    if len(_cache) > 500:
        _cache.pop(min(_cache, key=lambda k: _cache[k][1]), None)


# ───────────────────────── CLIENT HTTP ─────────────────────────
async def add_referer(request: httpx.Request):
    """Cloudflare renvoie 403 sur episodes.js sans Referer."""
    url = str(request.url)
    if "episodes.js" in url:
        request.headers["Referer"] = url.split("episodes.js")[0]
    elif "Referer" not in request.headers:
        request.headers["Referer"] = site_url


@asynccontextmanager
async def lifespan(app: FastAPI):
    global api, client, site_url

    url = os.getenv("ANIME_SAMA_SITE", "").strip()
    if not url:
        try:
            url = (await find_site_url()) or ""
        except Exception as e:
            print(f"⚠️ find_site_url a échoué : {e}")
    site_url = (url or "https://anime-sama.to/").rstrip("/") + "/"
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ───────────────────────── OUTILS TEXTE ─────────────────────────
def slugify(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def tokens(text: str) -> list[str]:
    return [t for t in slugify(text).split("-") if t and t not in STOP_TOKENS]


def pretty_slug(slug: str) -> str:
    return slug.replace("-", " ").title()


def relevance(query: str, slug: str, title: str = "") -> int:
    """Score de ressemblance requête <-> série. 0 = pas du tout pertinent."""
    if slugify(query) == slug:
        return 1000
    q = set(tokens(query))
    s = set(tokens(slug)) | set(tokens(title))
    if not q or not s:
        return 0
    inter = len(q & s)
    if inter == 0:
        return 0
    cov_q = inter / len(q)
    cov_s = inter / len(s)
    if cov_q >= 0.6 or (cov_s >= 0.99 and inter >= 2):
        return int(100 * cov_q + 50 * cov_s)
    return 0


# ───────────────────────── RECHERCHE ─────────────────────────
LINK_RE = re.compile(r'href="(?:https?://[^"/]+)?/?catalogue/([^"/?#]+)/?"')


def parse_search_results(page: str) -> list[dict]:
    matches = list(LINK_RE.finditer(page))
    results: list[dict] = []
    seen: set[str] = set()

    for i, m in enumerate(matches):
        slug = m.group(1)
        if slug in seen:
            continue
        seen.add(slug)

        # Le "morceau" de page qui appartient à cette carte
        end = len(page)
        for nxt in matches[i + 1:]:
            if nxt.group(1) != slug:
                end = nxt.start()
                break
        snippet = page[m.end(): min(end, m.end() + 2500)]

        title = None
        mh = re.search(r"<h2[^>]*>(.*?)</h2>", snippet, re.S | re.I)
        if mh:
            cand = unescape(re.sub(r"<[^>]+>", "", mh.group(1))).strip()
            if cand and set(tokens(cand)) & set(tokens(slug)):
                title = cand

        langs: list[str] = []
        mf = re.search(r'lang-flags">(.*?)</div', snippet, re.S | re.I)
        if mf:
            flags = [f.lower() for f in re.findall(r'title="(.+?)"', mf.group(1))]
            langs = ["vostfr"] + (["vf"] if "fr" in flags else [])

        results.append(
            {
                "title": title or pretty_slug(slug),
                "slug": slug,
                "url": f"{site_url}catalogue/{slug}/",
                "langs": langs,
            }
        )
    return results


async def search_catalogue(query: str) -> list[dict]:
    key = f"search|{query.strip().lower()}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    r = await client.get(f"{site_url}catalogue/", params={"search": query})
    r.raise_for_status()
    items = parse_search_results(r.text)
    items.sort(key=lambda it: relevance(query, it["slug"], it["title"]), reverse=True)
    cache_set(key, items, SEARCH_TTL)
    return items


async def find_catalogue(query: str) -> Catalogue | None:
    items = await search_catalogue(query)
    best, best_score = None, 0
    for it in items:
        score = relevance(query, it["slug"], it["title"])
        if score > best_score:
            best, best_score = it, score
    if not best:
        return None
    return Catalogue(url=best["url"], name=best["title"], client=client)


# ───────────────────────── SAISONS / ÉPISODES ─────────────────────────
def season_start(name: str) -> int | None:
    m = re.search(r"Episodes?\s*(\d+)", name, re.IGNORECASE)
    return int(m.group(1)) if m else None


def pick_season(seasons, episode: int, saison_num: int | None):
    """Retourne (saison, index de l'épisode dans cette saison)."""
    if saison_num is not None:
        found = [
            s for s in seasons
            if re.search(rf"saison\s*{saison_num}\b", s.name, re.IGNORECASE)
        ]
        if not found:
            raise HTTPException(
                404, f"Saison {saison_num} introuvable : {[s.name for s in seasons]}"
            )
        return found[0], episode - 1

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


# ───────────────────────── LANGUES / LECTEURS ─────────────────────────
SHORT_LANG = {"VF": "vf", "VOSTFR": "vostfr"}


def short_langs(ep) -> list[str]:
    available = ep.languages.availables
    return [SHORT_LANG[l] for l in ("VF", "VOSTFR") if l in available]


def candidates_by_lang(ep, wanted: str) -> list[tuple[str, list[str]]]:
    """[(langue, [urls de lecteurs])] — langue demandée d'abord, puis les autres."""
    available = ep.languages.availables
    order = [wanted] + [l for l in ("VF", "VOSTFR") if l != wanted]
    order += [l for l in available if l not in order]

    groups: list[tuple[str, list[str]]] = []
    for lang in order:
        urls: list[str] = []
        for players in available.get(lang, []):
            for url in players.sort_and_filter(PREFER_HOSTS, []):
                if url not in urls:
                    urls.append(url)
        if urls:
            groups.append((lang, urls))
    return groups


# ───────────────────────── EXTRACTION DU FLUX ─────────────────────────
def _extract_sync(embed_url: str) -> dict | None:
    if yt_dlp is None:
        return None
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 10,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(embed_url, download=False)

    url = info.get("url")
    if not url and info.get("formats"):
        url = info["formats"][-1].get("url")
    if not url:
        return None

    is_hls = ".m3u8" in url or "m3u8" in str(info.get("protocol", ""))
    headers = dict(info.get("http_headers") or {})
    headers.setdefault("Referer", embed_url)
    return {"url": url, "type": "hls" if is_hls else "mp4", "headers": headers}


async def extract_stream(embed_url: str) -> dict | None:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_extract_sync, embed_url), timeout=EXTRACT_TIMEOUT
        )
    except Exception as e:
        print(f"    ⚠️ extraction impossible ({embed_url}) : {str(e)[:120]}")
        return None


async def extract_first(embeds: list[str], deadline: float):
    """Teste les lecteurs par lots en parallèle ; retourne (embed, flux) ou None."""

    async def attempt(embed: str):
        return embed, await extract_stream(embed)

    for i in range(0, len(embeds), PARALLEL_EXTRACT):
        remaining = deadline - time.monotonic()
        if remaining <= 2:
            return None

        tasks = [asyncio.create_task(attempt(e)) for e in embeds[i:i + PARALLEL_EXTRACT]]
        try:
            for fut in asyncio.as_completed(tasks, timeout=remaining):
                try:
                    embed, stream = await fut
                except Exception:
                    continue
                if stream:
                    for t in tasks:
                        t.cancel()
                    return embed, stream
        except (asyncio.TimeoutError, TimeoutError):
            pass
        for t in tasks:
            t.cancel()
    return None


# ───────────────────────── ROUTES ─────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "api_ready": api is not None,
        "yt_dlp": yt_dlp is not None,
        "site": site_url,
    }


@app.get("/search")
async def search(
    q: str = Query(..., min_length=2),
    limit: int = Query(12, ge=1, le=30),
):
    if api is None:
        raise HTTPException(503, "API non initialisée")
    try:
        items = await search_catalogue(q)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Anime-Sama injoignable : {e}")
    return {"success": bool(items), "query": q, "results": items[:limit]}


@app.get("/resolve")
async def resolve(
    title: str = Query(...),
    episode: int = Query(..., ge=1),
    langue: str = Query("vostfr"),
    saison: int | None = Query(
        None, ge=1, description="Si fourni, 'episode' est relatif à cette saison"
    ),
):
    if api is None:
        raise HTTPException(503, "API non initialisée")

    wanted = langue.strip().upper()
    print(f"\n🎬 {title} | Ep {episode} | {wanted} | saison={saison}")

    cache_key = f"res|{slugify(title)}|{episode}|{wanted}|{saison}"
    cached = cache_get(cache_key)
    if cached:
        print("  ⚡ cache")
        return {**cached, "cached": True}

    try:
        cat = await find_catalogue(title)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Anime-Sama injoignable : {e}")
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
        raise HTTPException(
            404,
            f"Épisode hors limites ({len(episodes)} épisodes dans '{season.name}')",
        )
    ep = episodes[idx]

    groups = candidates_by_lang(ep, wanted)
    if not groups:
        raise HTTPException(404, "Aucun lecteur pour cet épisode")
    print(f"  🎞️ langues : {[(l, len(u)) for l, u in groups]}")

    base = {
        "success": True,
        "anime": cat.name,
        "anime_url": cat.url,
        "saison": season.name,
        "episode": episode,
        "episode_name": ep.name,
        "langue_demandee": wanted,
        "langues_disponibles": short_langs(ep),
    }

    deadline = time.monotonic() + RESOLVE_BUDGET
    for lang, embeds in groups:
        found = await extract_first(embeds, deadline)
        if found:
            embed, stream = found
            print(f"  ✅ flux {stream['type']} via {urlparse(embed).hostname} ({lang})")
            resp = {
                **base,
                "langue": lang,
                "lecteur": embed,
                "stream_url": stream["url"],
                "stream_type": stream["type"],
                "stream_headers": stream["headers"],
                "embed_urls": embeds,
                "embed_url": embed,
            }
            cache_set(cache_key, resp)
            return resp
        if time.monotonic() >= deadline:
            break

    # Aucun flux direct : on renvoie les pages de lecteurs (iframe)
    lang0, embeds0 = groups[0]
    print(f"  ⚠️ aucun flux direct, {len(embeds0)} embeds ({lang0})")
    resp = {
        **base,
        "langue": lang0,
        "lecteur": embeds0[0],
        "stream_url": None,
        "stream_type": None,
        "stream_headers": {},
        "embed_urls": embeds0,
        "embed_url": embeds0[0],
    }
    cache_set(cache_key, resp, ttl=5 * 60)
    return resp


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))