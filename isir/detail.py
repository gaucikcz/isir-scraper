"""Detail veci (oddil B) - chronologie udalosti a odkazy na dokumenty."""
import logging
import re
from datetime import date, datetime
from typing import List, Optional

from bs4 import BeautifulSoup

from . import config, http

log = logging.getLogger(__name__)

_WS = re.compile(r"\s+")
_CODE_RE = re.compile(r"^[A-Z]_\d+$")
_DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
_DOCID_RE = re.compile(r"[?&]id=(\d+)")


def _clean(node) -> str:
    if node is None:
        return ""
    return _WS.sub(" ", node.get_text(" ", strip=True)).strip()


def _parse_date(text: str) -> Optional[date]:
    m = _DATE_RE.match(text.strip())
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def parse_events(html: str) -> List[dict]:
    """Rozparsuje tabulku udalosti.

    Radky se identifikuji podle bunky obsahujici kod udalosti (napr. 'I_347') -
    ta je ve skryte bunce a je spolehlivejsi nez cesky nazev akce.
    Odkaz na dokument se bere VZDY z konkretniho <tr> daneho radku (kap. 5:
    naivni vyber poslednich N odkazu na strance vraci spatny dokument).
    """
    soup = BeautifulSoup(html, "lxml")
    events = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        if not cells:
            cells = tr.find_all(["td", "th"], recursive=False)
        if len(cells) < 4:
            continue

        texts = [_clean(c) for c in cells]

        kod = None
        for t in texts:
            if _CODE_RE.match(t):
                kod = t
                break
        if kod is None:
            continue  # neni to radek udalosti

        udalost_datum = None
        cas = None
        for t in texts:
            if udalost_datum is None:
                d = _parse_date(t)
                if d is not None:
                    udalost_datum = d
                    continue
            if cas is None and _TIME_RE.match(t):
                cas = t

        # popis = nejdelsi textova bunka, ktera neni kod/datum/cas/poradi
        popis = ""
        for t in texts:
            if t in (kod, cas) or _DATE_RE.match(t) or re.match(r"^\d+\.$", t):
                continue
            if len(t) > len(popis):
                popis = t

        doc_ids = []
        for a in tr.find_all("a", href=True):
            m = _DOCID_RE.search(a["href"])
            if m and "dokument.PDF" in a["href"]:
                doc_ids.append(m.group(1))

        # duplicity uvnitr radku zahodit, poradi zachovat
        uniq = []
        for d in doc_ids:
            if d not in uniq:
                uniq.append(d)

        doc_id = uniq[0] if uniq else None
        doc_id_side = uniq[1] if len(uniq) > 1 else None

        events.append(
            {
                "kod": kod,
                "datum": udalost_datum,
                "cas": cas,
                "popis": popis,
                "doc_id": doc_id,
                "doc_url": ("%s?id=%s" % (config.DOC_URL, doc_id)) if doc_id else None,
                "doc_id_side": doc_id_side,
                "doc_url_side": ("%s?id=%s" % (config.DOC_URL, doc_id_side)) if doc_id_side else None,
            }
        )
    return events


def fetch_events(detail_id: str) -> List[dict]:
    """Stahne cely oddil B jednim requestem (pageB=all) a vrati udalosti."""
    html = http.get_html(
        config.DETAIL_URL,
        params={
            "id": detail_id,
            "actSheet": "B",
            "pageA": "1",
            "pageC": "1",
            "pageD": "0",
            "pageP": "1",
            "pageB": "all",
        },
    )
    events = parse_events(html)
    log.debug("Detail %s: %d udalosti", detail_id, len(events))
    return events


def find_events(events: List[dict], kod: str, on_date: Optional[date] = None) -> List[dict]:
    """Vyfiltruje udalosti daneho kodu, volitelne k jednomu datu."""
    out = [e for e in events if e["kod"] == kod]
    if on_date is not None:
        out = [e for e in out if e["datum"] == on_date]
    return out


def nearest_preceding(events: List[dict], codes, before: Optional[date]) -> Optional[dict]:
    """Nejblizsi predchozi udalost s dokumentem z dane skupiny kodu (kap. 7)."""
    best = None
    for e in events:
        if e["kod"] not in codes or not e["doc_id"] or e["datum"] is None:
            continue
        if before is not None and e["datum"] > before:
            continue
        if best is None or e["datum"] > best["datum"]:
            best = e
    return best
