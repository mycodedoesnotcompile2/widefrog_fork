import base64
import builtins
import json
import re
from os.path import join
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from utils.constants.macros import ERR_MSG, USER_ERROR, APP_ERROR
from utils.main_service import main_service
from utils.structs import BaseElement, BaseService, CustomException
from utils.tools.args import check_range
from utils.tools.common import flatten_list, get_valid_filename, get_ext_from_url


class kijk_nl(BaseService):
    DEMO_URLS = [
        "https://www.kijk.nl/programmas/lang-leve-de-liefde",
        "https://www.kijk.nl/programmas/lingo",
        "https://www.kijk.nl/programmas/vrienden-van-lingo",
        "https://www.kijk.nl/programmas/het-jachtseizoen",
        "https://www.kijk.nl/programmas/camping-de-wildernis/QBhATP8rokD",
        "https://www.kijk.nl/programmas/10-voor-taal/BPSOLHDAwPp",
        "https://www.kijk.nl/films/lock-stock-and-two-smoking-barrels/qw9q3GjAQu7",
        "https://www.kijk.nl/films/venom/D3mFkhATspI",
        "https://www.kijk.nl/programmas/vrienden-van-lingo/krnoeBgBKQrCbl",
    ]

    # Kijk.nl's GraphQL backend has moved to this Talpa endpoint.
    GRAPHQL_URL = "https://api.prd.video.talpa.network/graphql"
    BASE_URL = "https://www.kijk.nl"

    DRM_TOKEN = None
    MAN_PRIORITY = {"": 0, "m3u8": 1, "ism": 2, "dash": 3}
    PAGE_SIZE = 50

    # Headers that mimic a real browser request to both the Talpa API
    # and the VUDRM license proxy (CORS checks require Origin/Referer).
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:159.0) "
            "Gecko/20100101 Firefox/159.0"
        ),
        "Accept": "*/*",
        "Accept-Language": "nl",
        "Content-Type": "text/plain;charset=UTF-8",
        "Referer": "https://www.kijk.nl/",
        "Origin": "https://www.kijk.nl",
    }

    @staticmethod
    def test_service():
        main_service.run_service(kijk_nl)

    @staticmethod
    def credentials_needed():
        return {"NONE_NEEDED": True}

    @staticmethod
    def get_site_info():
        graphql_url = kijk_nl.GRAPHQL_URL

        # Fetch a JWT for the VUDRM proxy. The provider must be "JWP";
        # without it the response has a null drmToken.
        token_payload = {
            "query": (
                "query DrmTokenQuery($provider: DrmProvider) {\n"
                "  drmToken(drmProvider: $provider) {\n"
                "    expiration\n"
                "    token\n"
                "  }\n"
                "}"
            ),
            "variables": {"provider": "JWP"},
        }
        token_resp = requests.post(
            graphql_url, headers=kijk_nl.HEADERS, json=token_payload
        )
        token_resp.raise_for_status()

        data = token_resp.json().get("data", {}).get("drmToken") or {}
        drm_token = data.get("token")
        if not drm_token:
            raise CustomException(ERR_MSG.format(
                type=APP_ERROR,
                url=graphql_url,
                reason="drmToken endpoint returned no token",
                solution=f"Update the {kijk_nl.__name__} service",
            ))

        return graphql_url, drm_token

    @staticmethod
    def initialize_service():
        if kijk_nl.GRAPHQL_URL is None or kijk_nl.DRM_TOKEN is None:
            kijk_nl.GRAPHQL_URL, kijk_nl.DRM_TOKEN = kijk_nl.get_site_info()
        return kijk_nl

    @staticmethod
    def get_keys(challenge, additional):
        # VUDRM's /proxy endpoint expects:
        #   - Header X-VUDRM-TOKEN: <JWT from DrmTokenQuery>
        #   - Content-Type: application/octet-stream
        #   - Body: the *raw* Widevine challenge (not base64, not JSON)
        #   - The release id as a query parameter (contentId)
        license_url = additional["license_url"]
        release_pid = additional.get("release_pid") or ""
        if release_pid:
            sep = "&" if "?" in license_url else "?"
            license_url = f"{license_url}{sep}contentId={release_pid}"

        licence = requests.post(
            license_url,
            headers={
                "User-Agent": kijk_nl.HEADERS["User-Agent"],
                "Accept": "*/*",
                "Accept-Language": "nl",
                "Origin": "https://www.kijk.nl",
                "Referer": "https://www.kijk.nl/",
                "Content-Type": "application/octet-stream",
                "X-VUDRM-TOKEN": kijk_nl.DRM_TOKEN,
            },
            data=challenge,
        )
        licence.raise_for_status()
        return licence.content

    @staticmethod
    def get_video_data(source_element):
        guid = source_element.url.split("/")[-1]
        page_dict = json.loads(re.findall(
            r'type="application/json"[^<>]*>({.*?})</script>',
            requests.get(source_element.url).content.decode(),
        )[0]).get("props", {})

        response = (
            page_dict.get("pageProps", {})
            .get("formatDataInitial", {})
            .get("video", {})
        )
        if response.get("guid", None) != guid:
            response = page_dict.get("apolloState", {})

            for k, v in response.items():
                if not k.startswith("Program:"):
                    continue
                if v.get("guid", None) != guid:
                    continue

                response = v
                break

        if source_element.element is None:
            title = response.get("title", None)
            if title in ["", None]:
                title = response.get("slug", None)
            title = get_valid_filename(title)
            if title is None:
                title = guid
            source_element.element = title
        if source_element.collection is None:
            source_element.collection = join(
                str(builtins.CONFIG["DOWNLOAD_COMMANDS"]["OUTPUT_MEDIA_PATH"]),
                kijk_nl.__name__,
            )

        subtitles = []
        index = 0
        output_path = join(source_element.collection, source_element.element)

        for track in response.get("tracks", []):
            if track.get("kind", "").lower() not in ["captions"]:
                continue
            index += 1

            srt_url = track["file"]
            srt_ext = get_ext_from_url(srt_url)
            srt_code = get_valid_filename(track["label"].lower())

            subtitles.append(
                (
                    False,
                    BaseElement(
                        url=srt_url,
                        collection=output_path,
                        element=f"subtitle_{index}_{srt_code}{srt_ext}",
                    ),
                )
            )

        additional = {"SUBTITLES": subtitles}

        # Sources query — POST instead of GET to match the new endpoint.
        sources_payload = {
            "operationName": "sources",
            "variables": {"guid": guid},
            "query": """
                query sources($guid:[String]) {
                    programs(guid:$guid) { items { guid sources { type file drm } } }
                }
            """,
        }
        sources_resp = requests.post(
            kijk_nl.GRAPHQL_URL, headers=kijk_nl.HEADERS, json=sources_payload
        )
        sources_resp.raise_for_status()

        response = sources_resp.json()["data"]["programs"]["items"]
        response = [r["sources"] for r in response if r["guid"] == guid]
        response = flatten_list(response)

        if len(response) == 0:
            raise CustomException(ERR_MSG.format(
                type=f"{USER_ERROR}",
                url=source_element.url,
                reason="The content isn't available",
                solution="Do not attempt to download it",
            ))

        response = list(filter(
            lambda r: len(parse_qs(urlparse(r["file"]).query).keys()) > 0,
            response,
        ))
        if len(response) == 0:
            raise CustomException(ERR_MSG.format(
                type=f"{USER_ERROR}",
                url=source_element.url,
                reason="Need Dutch IP to access content",
                solution="Use a VPN",
            ))

        response = (
            list(filter(lambda r: r.get("drm", {}) in [None, {}], response))
            + list(filter(
                lambda r: len(list(filter(
                    lambda d: "widevine" in d.lower(),
                    list(r["drm"].keys()),
                ))) > 0,
                list(filter(
                    lambda r: r.get("drm", {}) not in [None, {}], response
                )),
            ))
        )
        if len(response) == 0:
            raise CustomException(ERR_MSG.format(
                type=USER_ERROR,
                url=source_element.url,
                reason="DRM not supported. Widevine wasn't found",
                solution="Do not attempt to download it",
            ))

        response = sorted(
            response,
            reverse=True,
            key=lambda r: kijk_nl.MAN_PRIORITY.get(r["type"].lower(), ""),
        )
        if (
            response[0]["type"].lower() not in ["dash", "ism"]
            and response[0].get("drm", {}) not in [{}, None]
        ):
            raise CustomException(ERR_MSG.format(
                type=APP_ERROR,
                url=source_element.url,
                reason=(
                    f"Manifest format not supported: {str(response)}. "
                    f"Can't extract pssh"
                ),
                solution=f"Extend the {kijk_nl.__name__} service",
            ))

        response = response[0]
        manifest = response["file"]
        try:
            if response.get("drm", {}) in [None, {}]:
                raise
            for k, v in response["drm"].items():
                if "widevine" in k.lower():
                    additional["license_url"] = v["url"]
                    additional["release_pid"] = v["releasePid"]
                    break

            pssh_value = str(min(
                re.findall(
                    r"<cenc:pssh\b[^>]*>(.*?)</cenc:pssh>",
                    requests.get(manifest).content.decode(),
                ),
                key=len,
            ))
        except Exception:
            pssh_value = None

        return manifest, pssh_value, additional

    @staticmethod
    def _get_content_api_items(endpoint):
        items = []
        skip = 0

        while True:
            response = requests.get(
                endpoint,
                headers=kijk_nl.HEADERS,
                params={"skip": skip, "limit": kijk_nl.PAGE_SIZE},
            )
            response.raise_for_status()
            data = response.json()["data"]
            page_items = data.get("items", [])
            items.extend(page_items)

            skip += len(page_items)
            if skip >= data["total"]:
                return items
            if len(page_items) == 0:
                raise CustomException(ERR_MSG.format(
                    type=APP_ERROR,
                    url=endpoint,
                    reason="The content API returned an incomplete result",
                    solution=f"Debug the {kijk_nl.__name__} service",
                ))

    @staticmethod
    def get_collection_elements(collection_url):
        collection_url = collection_url.rstrip("/")
        if "/programmas/" in collection_url or "/films/" in collection_url:
            if collection_url.split(kijk_nl.BASE_URL)[-1].count("/") > 2:
                return [BaseElement(url=collection_url)]
        if "/programmas/" not in collection_url:
            return None

        slug = collection_url.split("/")[-1]
        page_html = requests.get(collection_url).content.decode()
        page_props = None
        for script in re.findall(
            r'type="application/json"[^<>]*>({.*?})</script>', page_html
        ):
            try:
                page_data = json.loads(script)
            except json.JSONDecodeError:
                continue
            page_props = page_data.get("props", {}).get("pageProps")
            if page_props is not None:
                break

        current_series = (
            page_props.get("initialSeries")
            if isinstance(page_props, dict)
            else None
        )
        if isinstance(current_series, dict):
            if slug != current_series.get("slug", ""):
                return []

            collection_name = get_valid_filename(
                current_series.get("title", slug)
            )
            collection_name = join(
                join(
                    str(builtins.CONFIG["DOWNLOAD_COMMANDS"]["OUTPUT_MEDIA_PATH"]),
                    kijk_nl.__name__,
                ),
                collection_name,
            )

            season_numbers = {
                int(season["seasonNumber"])
                for season in page_props.get("initialSeasons", [])
                if season.get("seasonNumber") is not None
            }

            episodes = kijk_nl._get_content_api_items(
                current_series["episodesEndpoint"]
            )
            collection = []
            episode_indexes = {}

            for episode in episodes:
                subtitle = episode.get("subtitle", "")
                season_match = re.search(
                    r"(?:seizoen|season)\s*(\d+)", subtitle, re.IGNORECASE
                )
                if season_match is None:
                    raise CustomException(ERR_MSG.format(
                        type=APP_ERROR,
                        url=collection_url,
                        reason=f"Couldn't determine the season for episode {episode.get('id', '')}",
                        solution=f"Debug the {kijk_nl.__name__} service",
                    ))

                season_number = int(season_match.group(1))
                season_numbers.add(season_number)
                season_title = f"Season_{season_number}"
                collection_check = check_range(
                    True, season_number, None, is_asc=False
                )
                if collection_check is not None:
                    continue

                episode_indexes[season_number] = (
                    episode_indexes.get(season_number, 0) + 1
                )
                episode_index = episode_indexes[season_number]

                check = check_range(False, season_number, episode_index)
                if check is not None:
                    continue

                episode_match = re.search(
                    r"(?:aflevering|episode)\s*(\d+)",
                    subtitle,
                    re.IGNORECASE,
                )
                episode_number = (
                    int(episode_match.group(1))
                    if episode_match is not None
                    else episode_index
                )
                episode_url = episode.get("url") or (
                    f"{collection_url}/{episode.get('videoGuid') or episode['id']}"
                )
                collection.append(BaseElement(
                    url=urljoin(kijk_nl.BASE_URL, episode_url),
                    collection=join(collection_name, season_title),
                    element=f"Episode_{episode_number}",
                ))

            clips_endpoint = current_series.get("clipsEndpoint")
            if clips_endpoint:
                clip_season = max(season_numbers, default=0) + 1
                collection_check = check_range(
                    True, clip_season, None, is_asc=False
                )
                if collection_check is None:
                    clip_title = f"Season_{clip_season}_Clips"
                    for clip_index, clip in enumerate(
                        kijk_nl._get_content_api_items(clips_endpoint), start=1
                    ):
                        check = check_range(
                            False, clip_season, clip_index
                        )
                        if check is not None:
                            continue

                        title = get_valid_filename(
                            clip.get("title")
                            or clip.get("subtitle")
                            or clip.get("videoGuid")
                            or clip.get("id")
                        )
                        clip_url = clip.get("url") or (
                            f"{collection_url}/{clip.get('videoGuid') or clip['id']}"
                        )
                        collection.append(BaseElement(
                            url=urljoin(kijk_nl.BASE_URL, clip_url),
                            collection=join(collection_name, clip_title),
                            element=f"Episode_{clip_index}_{title}",
                        ))

            return collection

        response = (
            json.loads(re.findall(
                r'type="application/json"[^<>]*>({.*?})</script>',
                page_html,
            )[0])
            .get("props", {})
            .get("pageProps", {})
            .get("formatDataInitial", {})
            .get("format", {})
        )

        if slug != response.get("slug", ""):
            return []
        collection_name = get_valid_filename(response.get("title", slug))
        collection_name = join(
            join(
                str(builtins.CONFIG["DOWNLOAD_COMMANDS"]["OUTPUT_MEDIA_PATH"]),
                kijk_nl.__name__,
            ),
            collection_name,
        )

        seasons = []
        for s1 in ["seasonList", "seriesTvSeasons"]:
            s2 = response.get(s1, [])
            if len(s2) == 0:
                continue

            for s3 in s2:
                season_id = s3.get("value", "")
                if len(season_id) == 0:
                    season_id = s3["id"].split("/")[-1]

                season_number = s3.get("seasonNumber", 0)
                season_title = s3["guid"]
                for st in ["title", "label", "ariaLabel"]:
                    if s3.get(st, "") not in [None, ""]:
                        season_title = s3[st]
                        break

                season_title = get_valid_filename(
                    f"Season_{season_number}_{season_title}"
                )
                seasons.append((
                    season_id,
                    season_number,
                    season_title,
                    "tvSeasonId",
                    "EPISODE",
                ))
            break

        seasons = sorted(seasons, key=lambda season: season[1])
        season_index = 0
        if len(seasons) > 0:
            season_index = seasons[-1][1]
        seasons.append((
            response["id"],
            season_index + 1,
            f"Season_{season_index + 1}_Clips",
            "seriesId",
            "CLIP",
        ))

        content_query = """
            query programs(
                $tvSeasonId: String, $programTypes: [ProgramType], $seriesId: String,
                $skip: Int, $limit: Int, $sort: ProgramSortKey, $guid: [String]
            ) {
                programs(
                    tvSeasonId: $tvSeasonId, programTypes: $programTypes, seriesId: $seriesId,
                    skip: $skip, limit: $limit, sort: $sort, guid: $guid
                ) { totalResults items { guid slug title } }
            }
        """

        collection = []
        for (
            season_id,
            season_number,
            season_title,
            id_type,
            program_type,
        ) in seasons:
            check = check_range(True, season_number, None)
            if check is True:
                continue
            elif check is False:
                return collection

            skip = 0
            episode_index = 0

            while True:
                variables = {
                    "programTypes": program_type,
                    "skip": skip,
                    "limit": kijk_nl.PAGE_SIZE,
                    id_type: season_id,
                }
                if program_type == "CLIP":
                    variables["sort"] = "PUBLICATIONDATETIME"

                request_payload = {
                    "query": content_query,
                    "operationName": "programs",
                    "variables": variables,
                }
                resp = requests.post(
                    kijk_nl.GRAPHQL_URL, headers=kijk_nl.HEADERS, json=request_payload
                )
                resp.raise_for_status()

                response = resp.json()["data"]["programs"].get("items", [])
                if len(response) == 0:
                    break
                skip += kijk_nl.PAGE_SIZE

                for episode in response:
                    episode_index += 1
                    check = check_range(False, season_number, episode_index)
                    if check is True:
                        continue
                    elif check is False:
                        return collection

                    episode_title = episode["guid"]
                    for et in ["title", "slug"]:
                        if episode.get(et, "") not in [None, ""]:
                            episode_title = episode[et]
                            break
                    episode_title = get_valid_filename(episode_title)

                    collection.append(BaseElement(
                        url=collection_url + "/" + episode["guid"],
                        collection=join(collection_name, season_title),
                        element=f"Episode_{episode_index}_{episode_title}",
                    ))

        return collection