"""Download pinned eCFR snapshots (Versioner API) for sources that declare `fetch:`."""

from datetime import date
from pathlib import Path

import httpx
from lxml import etree
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.core.config import Settings
from app.ingestion.errors import FetchError
from app.ingestion.models import SourceSpec


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 500, 502, 503, 504)


@retry(retry=retry_if_exception(_retryable), stop=stop_after_attempt(4), wait=wait_exponential(min=2, max=30), reraise=True)
def _get(client: httpx.Client, url: str, params: dict | None = None) -> httpx.Response:
    response = client.get(url, params=params)
    response.raise_for_status()
    return response


def _client(settings: Settings) -> httpx.Client:
    # The eCFR API rejects uncompressed responses; httpx requests gzip by default.
    return httpx.Client(
        base_url=settings.ecfr_base_url,
        headers={"User-Agent": settings.http_user_agent},
        timeout=httpx.Timeout(60.0),
        follow_redirects=True,
    )


def latest_ecfr_date(title: int, settings: Settings) -> date:
    """Most recent date for which eCFR has the given title."""
    with _client(settings) as client:
        try:
            data = _get(client, "/api/versioner/v1/titles.json").json()
        except httpx.HTTPError as exc:
            raise FetchError(f"cannot query eCFR titles: {exc}") from exc
    for entry in data.get("titles", []):
        if entry.get("number") == title:
            return date.fromisoformat(entry["up_to_date_as_of"])
    raise FetchError(f"eCFR does not list title {title}")


def fetch_ecfr(source: SourceSpec, settings: Settings) -> Path:
    if source.fetch is None:
        raise FetchError(f"{source.id} has no fetch specification")
    spec = source.fetch
    target = settings.data_dir / source.path
    url = f"/api/versioner/v1/full/{spec.date.isoformat()}/title-{spec.title}.xml"
    with _client(settings) as client:
        try:
            content = _get(client, url, params={"part": spec.part}).content
        except httpx.HTTPError as exc:
            raise FetchError(f"{source.id}: download failed: {exc}") from exc

    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring(content, parser)
    except etree.XMLSyntaxError as exc:
        raise FetchError(f"{source.id}: response is not valid XML: {exc}") from exc
    if root.tag != "DIV5" or root.get("N") != str(spec.part):
        raise FetchError(f"{source.id}: response is not part {spec.part} (root {root.tag} N={root.get('N')})")

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(content)
    tmp.replace(target)
    return target
