"""eCFR Versioner API client: pinned snapshot downloads and update checks."""

from datetime import date
from pathlib import Path
from typing import Any

import httpx
from lxml import etree
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.ingestion.domain.errors import FetchError
from app.ingestion.domain.models import EcfrFetchSpec

_TITLES_PATH = "/api/versioner/v1/titles.json"
_FULL_TEXT_PATH = "/api/versioner/v1/full/{date}/title-{title}.xml"
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in _RETRYABLE_STATUS


class EcfrClient:
    def __init__(self, base_url: str, user_agent: str, timeout_seconds: float):
        self._base_url = base_url
        self._user_agent = user_agent
        self._timeout = timeout_seconds

    def latest_date(self, title: int) -> date:
        """Most recent date for which eCFR has the given title."""
        try:
            data = self._get(_TITLES_PATH).json()
        except httpx.HTTPError as exc:
            raise FetchError(f"cannot query eCFR titles: {exc}") from exc
        for entry in data.get("titles", []):
            if entry.get("number") == title:
                return date.fromisoformat(entry["up_to_date_as_of"])
        raise FetchError(f"eCFR does not list title {title}")

    def download_part(self, spec: EcfrFetchSpec, target: Path) -> Path:
        """Download one part at the pinned date, verify it, and write it atomically to `target`."""
        url = _FULL_TEXT_PATH.format(date=spec.date.isoformat(), title=spec.title)
        try:
            content = self._get(url, params={"part": spec.part}).content
        except httpx.HTTPError as exc:
            raise FetchError(f"title {spec.title} part {spec.part}: download failed: {exc}") from exc

        try:
            root = etree.fromstring(content, etree.XMLParser(resolve_entities=False, no_network=True))
        except etree.XMLSyntaxError as exc:
            raise FetchError(f"part {spec.part}: response is not valid XML: {exc}") from exc
        if root.tag != "DIV5" or root.get("N") != str(spec.part):
            raise FetchError(f"response is not part {spec.part} (root {root.tag} N={root.get('N')})")

        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(target.suffix + ".part")
        partial.write_bytes(content)
        partial.replace(target)
        return target

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(4),
        wait=wait_exponential(min=2, max=30),
        reraise=True,
    )
    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        # The eCFR API rejects uncompressed responses; httpx requests gzip by default.
        with httpx.Client(
            base_url=self._base_url,
            headers={"User-Agent": self._user_agent},
            timeout=httpx.Timeout(self._timeout),
            follow_redirects=True,
        ) as client:
            response = client.get(path, params=params)
            response.raise_for_status()
            return response
