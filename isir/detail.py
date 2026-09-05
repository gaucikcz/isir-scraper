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


def _order_key(event: dict, index: int):
    """Klic pro razeni udalosti: datum, cas (kdyz je), poradi na strance."""
    cas = event.get("cas")
    minutes = -1
    if cas and _TIME_RE.match(cas):
        hh, mm = cas.split(":")
        minutes = int(hh) * 60 + int(mm)
    return (event["datum"], minutes, index)


def latest_soupis(events: List[dict]) -> Optional[dict]:
    """Nejnovejsi soupis majetkove podstaty, ktery ma dokument (nebo None).

    Navrh na zpenezeni (I_347) casto popisuje majetek jen odkazem
    ("majetek zapsany v soupisu na c.d. 160"), takze skutecny seznam veci
    je jen v soupisu. Vec jich ma obvykle nekolik (puvodni, "- doplneni",
    "- zmena") a nas zajima ten NEJNOVEJSI = aktualni obraz podstaty.

    Pozor: nezamenovat s nearest_preceding(), ktery hleda soupis platny
    v dobe podani navrhu (cross-reference pro LLM).
    """
    best = None
    best_key = None
    for index, event in enumerate(events):
        if event.get("kod") not in config.SOUPIS_CODES:
            continue
        if not event.get("doc_id") or event.get("datum") is None:
            continue
        key = _order_key(event, index)
        if best_key is None or key > best_key:
            best, best_key = event, key
    return best


# ---------------------------------------------------------------------------
# Relevantni dokumenty veci (shortlist pro dashboard a dossier)
# ---------------------------------------------------------------------------

# MERENI (20 veci s vysokou prioritou): zadny JEDEN typ dokumentu neni
# spolehlive pritomny - usneseni o prodeji mimo drazbu 50 %, soupis majetkove
# podstaty 30 %, vypis z katastru 25 %, zprava spravce 25 %, znalecky posudek
# 0 %. Odkazovat jeden typ tedy nema smysl; misto toho drzime u KAZDE veci
# kratky SHORTLIST toho, co v oddilu B opravdu je. Detail veci uz stahujeme
# kvuli soupisu, takze to nestoji ani jeden request navic.
_KATASTR_RE = re.compile(r"katastr", re.I)
_ZPRAVA_RE = re.compile(r"zpr[áa]va", re.I)
# Druha podminka u "zpravy": samotne "zprava" chyti i zpravy o plneni splatkoveho
# kalendare. Zajima nas zprava SPRAVCE / o STAVU rizeni / pro schuzi ODDILU.
_ZPRAVA_KONTEXT_RE = re.compile(r"spr[áa]vc|stavu|odd[íi]?l", re.I)
_PRODEJ_RE = re.compile(r"usnesen[íi] o prodeji|smlouva o prodeji", re.I)

# (kategorie, kody udalosti, matcher na popis). Poradi = poradi ve vystupu
# i priorita pri stahovani v dossieru: navrh je kotva veci, pak co je v podstate
# (soupis), cim je to dolozeno (katastr), co k tomu rika spravce (zprava)
# a nakonec varovani, ze uz se neco prodava (prodej).
RELEVANT = (
    ("navrh", frozenset((config.EVENT_CODE,)), None),
    ("soupis", frozenset(config.SOUPIS_CODES), None),
    ("katastr", frozenset(("I_829",)), _KATASTR_RE.search),
    ("zprava", frozenset(("I_332",)),
     lambda popis: _ZPRAVA_RE.search(popis) and _ZPRAVA_KONTEXT_RE.search(popis)),
    # POZOR: "prodej" je VAROVANI - soud uz v teto veci nejaky prodej schvalil.
    # Neskryvat, dashboard to zvyraznuje.
    ("prodej", frozenset(config.CONFIRM_CODES), _PRODEJ_RE.search),
)

CATEGORY_ORDER = tuple(kategorie for kategorie, _codes, _matcher in RELEVANT)

# Kod udalosti je spolehlivejsi nez cesky nazev, takze se testuje driv nez
# regexy na popis - jinak by se napr. "Navrh na vydani usneseni o prodeji"
# (I_347) oznacil jako uz schvaleny prodej a vyrobil falesny poplach.
_CODE_TO_CATEGORY = {}
for _kategorie, _codes, _matcher in RELEVANT:
    for _kod in _codes:
        _CODE_TO_CATEGORY.setdefault(_kod, _kategorie)

# Zaloha pres popis. "prodej" se testuje prvni zamerne: kdyz popis mluvi
# zaroven o prodeji i o katastru, varovani ma prednost pred prilohou.
_POPIS_CATEGORIES = ("prodej", "katastr", "zprava")
_MATCHERS = dict((kategorie, matcher) for kategorie, _codes, matcher in RELEVANT)


def categorize(event: dict) -> Optional[str]:
    """Kategorie dokumentu ('navrh'/'soupis'/'katastr'/'zprava'/'prodej') nebo None."""
    kategorie = _CODE_TO_CATEGORY.get(event.get("kod") or "")
    if kategorie:
        return kategorie
    popis = event.get("popis") or ""
    if not popis:
        return None
    for kategorie in _POPIS_CATEGORIES:
        matcher = _MATCHERS.get(kategorie)
        if matcher is not None and matcher(popis):
            return kategorie
    return None


def relevant_documents(events: List[dict], limit_per_category: int = 3) -> List[dict]:
    """Shortlist dokumentu veci: {kod, datum, popis, url, kategorie}.

    V kazde kategorii nejnovejsi prvni a nejvyse limit_per_category kusu -
    detail veci ma bezne 100-650 radku a bez stropu by se do data.json nalilo
    osmdesat "Sdeleni". Udalosti bez dokumentu a bez data se preskakuji,
    stejny doc_id se vraci jen jednou (prvni vyskyt na strance).
    """
    buckets = {}
    seen = set()
    for index, event in enumerate(events or []):
        doc_id = event.get("doc_id")
        if not doc_id or event.get("datum") is None or doc_id in seen:
            continue
        kategorie = categorize(event)
        if kategorie is None:
            continue
        seen.add(doc_id)
        buckets.setdefault(kategorie, []).append((_order_key(event, index), event))

    out = []
    for kategorie in CATEGORY_ORDER:
        items = sorted(buckets.get(kategorie, []), key=lambda pair: pair[0], reverse=True)
        if limit_per_category is not None and limit_per_category > 0:
            items = items[:limit_per_category]
        for _key, event in items:
            out.append(
                {
                    "kod": event.get("kod"),
                    "datum": event["datum"].isoformat(),
                    "popis": event.get("popis") or "",
                    "url": "%s?id=%s" % (config.DOC_URL, event["doc_id"]),
                    "kategorie": kategorie,
                }
            )
    return out
