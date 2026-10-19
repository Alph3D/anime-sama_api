import asyncio
import logging
import re
from ast import literal_eval
from dataclasses import dataclass, replace
from functools import reduce
from typing import Any, cast, get_args

from httpx import AsyncClient

from .episode import Episode, Languages, Players
from .langs import LangId, flagid2lang, lang2ids
from .utils import remove_some_js_comments, split_and_strip, zip_varlen

logger = logging.getLogger(__name__)


@dataclass
class SeasonLangPage:
    lang_id: LangId
    html: str = ""
    episodes_js: str = ""


class Season:
    def __init__(
        self,
        url: str,
        name: str = "",
        serie_name: str = "",
        client: AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.site_url = "/".join(url.split("/")[:3]) + "/"

        self.name = name or url.split("/")[-2]
        self.serie_name = serie_name or url.split("/")[-3]

        self.client = client or AsyncClient()

    async def get_all_pages(self) -> list[SeasonLangPage]:
        async def process_page(lang_id: LangId) -> SeasonLangPage:
            page_url = self.url + lang_id + "/"
            response = await self.client.get(page_url)

            if response.is_error:
                return SeasonLangPage(lang_id=lang_id)

            html = response.text
            match_url = re.search(r"episodes\.js\?filever=\d+", html)

            if not match_url:
                return SeasonLangPage(lang_id=lang_id)

            # Cloudflare renvoie 403 sur episodes.js si le Referer est absent
            episodes_js = await self.client.get(
                page_url + match_url.group(0), headers={"Referer": page_url}
            )

            if episodes_js.is_error:
                return SeasonLangPage(lang_id=lang_id)

            return SeasonLangPage(
                lang_id=lang_id, html=html, episodes_js=episodes_js.text
            )

        pages = await asyncio.gather(
            *(process_page(lang_id) for lang_id in get_args(LangId))
        )
        pages_dict = {page.lang_id: page for page in pages}

        if pages_dict["vostfr"].html:
            found = re.findall(
                r"src=\".+flag_(.+?)\.png\".*?[\n\t]*<p.*?>VO</p>",
                remove_some_js_comments(pages_dict["vostfr"].html),
            )
            flag_id_vo = found[0].lower() if found else None

            # Si le drapeau VO n'est pas détecté (HTML du site modifié),
            # on ignore simplement cette étape facultative.
            if flag_id_vo and flag_id_vo in flagid2lang:
                for lang_id in lang2ids[flagid2lang[flag_id_vo]]:  # type: ignore
                    if not pages_dict[lang_id].html:
                        pages_dict[lang_id] = replace(
                            pages_dict["vostfr"]
                        )  # replace=copy
                        pages_dict[lang_id].lang_id = lang_id
                        break

        return [value for value in pages_dict.values() if value.html]

    # TODO: Refactor
    def _get_players_from(self, page: SeasonLangPage) -> list[Players]:
        players_list = re.findall(
            r"eps(\d+) ?= ?\[([\W\w]+?)\]", remove_some_js_comments(page.episodes_js)
        )
        # Tri numérique (eps10 doit venir après eps2)
        players_list = sorted(players_list, key=lambda tuple: int(tuple[0]))
        players_list_links = (
            cast(list[str], re.findall(r"'(.+?)'", player))
            for _, player in players_list
        )

        return [Players(players) for players in zip_varlen(*players_list_links)]

    def _get_episodes_names(
        self, page: SeasonLangPage, number_of_episodes: int, number_of_episodes_max: int
    ) -> list[str]:
        def padding(n: int) -> str:
            return " " * (len(str(number_of_episodes_max)) - len(str(n)))

        def episode_name_range(*args) -> list[str]:
            return [f"Episode {n}{padding(n)}" for n in range(*args)]

        def generic_names() -> list[str]:
            return episode_name_range(1, number_of_episodes + 1)

        blocks = re.findall(
            r"resetListe\(\);\s*(.*?)\}",
            remove_some_js_comments(page.html),
            re.DOTALL,
        )
        if not blocks:
            logger.warning(
                "resetListe() introuvable, noms génériques utilisés (%s)",
                self.url + page.lang_id,
            )
            return generic_names()

        functions_list = [
            f for f in split_and_strip(blocks[-1], (";", "\n")) if f
        ]

        episodes_name: list[str] = []
        try:
            for function in functions_list:
                if function.startswith("//"):
                    continue

                call_start = function.find("(")
                if call_start == -1:
                    continue
                function, args_sting = function[:call_start], function[
                    call_start + 1 : function.rfind(")")
                ]

                if args_sting.strip():
                    # Warning literal_eval: Can crash
                    args = literal_eval(node_or_string=args_sting + ",")
                    if type(args) is not tuple:
                        continue
                    args = cast(tuple, args)
                else:
                    args = ()

                match function:
                    case "":
                        continue
                    case "creerListe":
                        if len(args) < 2:
                            # Only seen on Dragon Ball GT (Film), Junji Ito Collection
                            # (Saison 1) and Orange (Film). Surely a small oversight
                            # in anime-sama, doing nothing is generally right.
                            continue

                        episodes_name += episode_name_range(
                            int(args[0]), int(args[1]) + 1
                        )
                    case "finirListe" | "finirListeOP":
                        if not args:
                            break

                        episodes_name += episode_name_range(
                            int(args[0]),
                            int(args[0]) + number_of_episodes - len(episodes_name),
                        )
                        break
                    case "newSP":
                        if not args:
                            raise NotImplementedError("newSP sans argument")
                        episodes_name.append(f"Episode {args[0]}")
                    case "newSPF":
                        if not args:
                            raise NotImplementedError("newSPF sans argument")
                        episodes_name.append(str(args[0]))
                    case name:
                        raise NotImplementedError(f"fonction inconnue '{name}'")
        except Exception as e:
            logger.warning(
                "Impossible de lire les noms d'épisodes (%s) : %s. "
                "Noms génériques utilisés.",
                self.url + page.lang_id,
                e,
            )
            return generic_names()

        if not episodes_name:
            return generic_names()

        # Si la liste de noms est plus courte que le nombre de lecteurs,
        # on complète pour ne pas perdre d'épisodes (zip tronquerait sinon).
        if len(episodes_name) < number_of_episodes:
            episodes_name += episode_name_range(
                len(episodes_name) + 1, number_of_episodes + 1
            )

        return episodes_name

    @staticmethod
    def _extend_episodes(
        current: list[tuple[str, Languages]],
        new: tuple[SeasonLangPage, list[str], list[Players]],
    ) -> list[tuple[str, Languages]]:
        """
        Extend a list of episodes AKA (name, languages) from a list names and players corresponding
        to a language while preserving the relative order of names.
        This function is intended to be used with reduce.
        """
        page, names, players_list = new  # Unpack args. This is due to reduce

        fusion = []
        curr_done = 0
        for name_new, players in zip(names, players_list):
            for pos, (name_current, languages) in enumerate(current[curr_done:]):
                if name_new == name_current:
                    languages[page.lang_id] = players
                    fusion.extend(current[curr_done : curr_done + pos + 1])
                    curr_done += pos + 1
                    break
            else:
                fusion.append((name_new, Languages({page.lang_id: players})))  # type: ignore
        fusion.extend(current[curr_done:])
        return fusion

    async def episodes(self) -> list[Episode]:
        pages = await self.get_all_pages()
        if not pages:
            return []

        players_list = [self._get_players_from(page) for page in pages]

        number_of_episodes_max = max(
            (len(episodes_page) for episodes_page in players_list), default=0
        )

        episodes_names = [
            self._get_episodes_names(page, len(episodes_page), number_of_episodes_max)
            for page, episodes_page in zip(pages, players_list)
        ]

        episodes: list[tuple[str, Languages]] = reduce(
            self._extend_episodes, zip(pages, episodes_names, players_list), []
        )

        return [
            Episode(
                languages,
                self.serie_name,
                self.name,
                name,
                index,
            )
            for index, (name, languages) in enumerate(episodes, start=1)
        ]

    def __repr__(self) -> str:
        return f"Season({self.name!r}, {self.serie_name!r})"

    def __str__(self) -> str:
        return self.name

    def __eq__(self, value: Any) -> bool:
        if not isinstance(value, Season):
            return False
        return self.url == value.url