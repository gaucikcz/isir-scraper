"""HTTP vrstva - zdvorily klient s retry a rate-limitem."""
import logging
import time
from typing import Optional

import requests

from . import config

log = logging.getLogger(__name__)

_session = None
_last_request_at = [0.0]


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": config.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
                "Accept-Language": "cs,en;q=0.7",
            }
        )
        _session = s
    return _session


def _throttle() -> None:
    elapsed = time.time() - _last_request_at[0]
    wait = config.REQUEST_DELAY - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_request_at[0] = time.time()


def get(url: str, params: Optional[dict] = None, stream: bool = False) -> requests.Response:
    """GET s exponencialnim backoffem. Vyhazuje pri trvalem selhani."""
    last_exc = None
    for attempt in range(config.MAX_RETRIES):
        _throttle()
        try:
            resp = session().get(
                url, params=params, timeout=config.REQUEST_TIMEOUT, stream=stream
            )
            if resp.status_code == 200:
                return resp
            # 5xx a 429 stoji za opakovani, 4xx uz ne.
            if resp.status_code < 500 and resp.status_code != 429:
                resp.raise_for_status()
            last_exc = requests.HTTPError("HTTP %s pro %s" % (resp.status_code, url))
        except requests.RequestException as exc:  # sit, timeout, DNS...
            last_exc = exc
        backoff = 2 ** attempt
        log.warning("Request selhal (%s), pokus %d/%d, cekam %ss",
                    last_exc, attempt + 1, config.MAX_RETRIES, backoff)
        time.sleep(backoff)
    raise RuntimeError("Nepodarilo se stahnout %s: %s" % (url, last_exc))


def get_html(url: str, params: Optional[dict] = None) -> str:
    resp = get(url, params=params)
    # ISIR deklaruje UTF-8 v meta, requests obcas hada latin-1 z hlavicek.
    resp.encoding = resp.encoding or "utf-8"
    if (resp.encoding or "").lower() in ("iso-8859-1", "latin-1", "latin1"):
        resp.encoding = "utf-8"
    return resp.text


def get_pdf(doc_id: str) -> bytes:
    """Stahne PDF dokumentu. Odkazy funguji bez session/cookie."""
    resp = get(config.DOC_URL, params={"id": doc_id})
    return resp.content
