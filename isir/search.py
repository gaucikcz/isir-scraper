"""Vyhledavani v ISIR - vysledek_lustrace.do filtrovany na druh_kod_udalost."""
import logging
import re
from datetime import date, timedelta
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from . import config, http

log = logging.getLogger(__name__)

_WS = re.compile(r"\s+")
_COUNT_RE = re.compile(r"POČET NALEZENÝCH DLUŽNÍKŮ", re.I)


def _clean(node) -> str:
    if node is None:
        return ""
    return _WS.sub(" ", node.get_text(" ", strip=True)).strip()


def _cz(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def _base_params(date_from: date, date_to: date, event_code: str) -> Dict[str, str]:
    """Presne parametry overene na produkci (kap. 3 metodiky)."""
    return {
        "nazev_osoby": "", "jmeno_osoby": "", "ic": "", "datum_narozeni": "",
        "rc": "", "mesto": "", "cislo_senatu": "", "bc_vec": "", "rocnik": "",
        "id_osoby_puvodce": "", "druh_stav_konkursu": "",
        "datum_stav_od": "", "datum_stav_do": "",
        "aktualnost": "AKTUALNI_I_UKONCENA",
        "druh_kod_udalost": event_code,
        "datum_akce_od": _cz(date_from),
        "datum_akce_do": _cz(date_to),
        "nazev_osoby_f": "",
        "cislo_senatu_vsns": "", "druh_vec_vsns": "", "bc_vec_vsns": "", "rocnik_vsns": "",
        "cislo_senatu_icm": "", "bc_vec_icm": "", "rocnik_icm": "",
        "rowsAtOnce": str(config.ROWS_AT_ONCE),
        "captcha_answer": "",
        "spis_znacky_datum": "",
        "spis_znacky_obdobi": "14DNI",
    }


def _reported_count(soup) -> Optional[int]:
    """Vytahne 'POCET NALEZENYCH DLUZNIKU' pro kontrolu uplnosti."""
    label = soup.find(string=_COUNT_RE)
    if not label:
        return None
    cell = label.find_parent("td")
    if cell is None:
        return None
    sibling = cell.find_next_sibling("td")
    if sibling is None:
        return None
    m = re.search(r"\d+", _clean(sibling))
    return int(m.group(0)) if m else None


def _field(container, *labels) -> str:
    """Najde hodnotu podle popisku ve <th> (napr. 'IČ:'). Vraci '' kdyz chybi."""
    for th in container.find_all("th"):
        text = _clean(th).rstrip(":").strip()
        for want in labels:
            if text.lower() == want.lower().rstrip(":"):
                td = th.find_next_sibling("td")
                if td is not None:
                    return _clean(td)
    return ""


def _parse_spisova_znacka(container):
    """'KSPH 66 INS 23815 / 2012' -> ('KSPH 66 INS 23815/2012', 'Krajského soudu v Praze')."""
    raw = _field(container, "Spisová značka")
    if not raw:
        return "", ""
    soud = ""
    m = re.search(r"Vedená\s+u\s+(.+)$", raw)
    if m:
        soud = m.group(1).strip()
        raw = raw[: m.start()].strip()
    # sjednotit ' / ' na '/'
    znacka = re.sub(r"\s*/\s*", "/", raw).strip()
    znacka = _WS.sub(" ", znacka)
    return znacka, soud


def _parse_records(html: str) -> List[dict]:
    soup = BeautifulSoup(html, "lxml")
    records = []
    for link in soup.select('a[href*="evidence_upadcu_detail.do"]'):
        # Kazdy zaznam je v jednom <td class="underLined">.
        block = link.find_parent("td", class_="underLined")
        if block is None:
            block = link.find_parent("tr")
        if block is None:
            continue

        href = link.get("href", "")
        m = re.search(r"id=([0-9a-fA-F-]+)", href)
        if not m:
            continue
        detail_id = m.group(1)

        znacka, soud = _parse_spisova_znacka(block)
        jmeno = _field(block, "Jméno/název")
        ico = _field(block, "IČ")
        rc_raw = _field(block, "Rodné číslo / Datum nar.")
        # prazdne pole vypada jako '/' - normalizovat na ''
        if rc_raw.strip(" /") == "":
            rc_raw = ""
        sidlo = _field(block, "Sídlo společnosti", "Bydliště")
        stav = _field(block, "Stav řízení")

        or_link = block.select_one('a[href*="or.justice.cz"]')

        records.append(
            {
                "spisova_znacka": znacka,
                "soud": soud,
                "dluznik_jmeno": jmeno,
                "ico": ico or None,
                "rodne_cislo_raw": rc_raw or None,
                "sidlo": sidlo or None,
                "stav_rizeni": stav or None,
                "detail_id": detail_id,
                "detail_url": "%s?id=%s&actSheet=B&pageB=all" % (config.DETAIL_URL, detail_id),
                "or_url": or_link.get("href") if or_link else None,
            }
        )
    return records, _reported_count(soup)


def search_window(date_from: date, date_to: date, event_code: str = None) -> List[dict]:
    """Vrati dluzniky, u kterych v danem okne probehla sledovana akce.

    Okno delsi nez 30 dni ISIR odmita - rozdelime ho na kousky a spojime.
    """
    event_code = event_code or config.EVENT_CODE
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    span = (date_to - date_from).days
    if span >= config.MAX_WINDOW_DAYS:
        out, seen = [], set()
        cursor = date_from
        while cursor <= date_to:
            chunk_end = min(cursor + timedelta(days=config.MAX_WINDOW_DAYS - 1), date_to)
            for rec in search_window(cursor, chunk_end, event_code):
                if rec["detail_id"] not in seen:
                    seen.add(rec["detail_id"])
                    out.append(rec)
            cursor = chunk_end + timedelta(days=1)
        return out

    params = _base_params(date_from, date_to, event_code)
    html = http.get_html(config.SEARCH_URL, params=params)

    if "překročilo 30 dní" in html or "prekrocilo 30 dni" in html:
        raise ValueError("ISIR odmitl okno %s..%s (limit 30 dni)" % (date_from, date_to))

    records, reported = _parse_records(html)

    # Strankovani: odkaz "Další" drzi offset v session state, proto stejna session.
    guard = 0
    while reported is not None and len(records) < reported and guard < 40:
        soup = BeautifulSoup(html, "lxml")
        nxt = None
        for a in soup.find_all("a"):
            if _clean(a) == "Další" and "vysledek_lustrace" in (a.get("href") or ""):
                nxt = a.get("href")
                break
        if not nxt:
            break
        url = nxt if nxt.startswith("http") else config.BASE + nxt
        html = http.get_html(url)
        more, _ = _parse_records(html)
        known = set(r["detail_id"] for r in records)
        fresh = [r for r in more if r["detail_id"] not in known]
        if not fresh:
            break
        records.extend(fresh)
        guard += 1

    if reported is not None and len(records) != reported:
        log.warning(
            "Okno %s..%s: ISIR hlasi %d dluzniku, rozparsovano %d",
            date_from, date_to, reported, len(records),
        )
    log.info("Okno %s..%s: %d dluzniku", date_from, date_to, len(records))
    return records
