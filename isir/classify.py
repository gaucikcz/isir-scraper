# -*- coding: utf-8 -*-
"""Klasifikace majetku z textu navrhu. Jedine misto, kde se utraci LLM tokeny.

Bez ANTHROPIC_API_KEY degraduje na heuristiku - pipeline bezi dal.
"""
import json
import logging
import os
import re
import time
import unicodedata
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

MAX_INPUT_CHARS = 24000
TYPY = ("nemovity", "movity", "nehmotny")

# --- klicova slova (porovnava se na textu bez diakritiky, lowercase) -----------
NEMOVITY_KW = [
    "pozemek", "pozemk", "parc. c", "parcel", "katastr", "k.u.", "kat. uzemi",
    "list vlastnictv", "lv c.", "bytov jednotk", "bytova jednotka", "nebytov",
    "stavba", "budova", "rodinny dum", "rodinneho domu", "hala", "areal",
    "garaz", "spoluvlastnick podil na nemovit", "nemovit", "orna puda",
    "zastavena plocha", "trvaly travni porost", "chata", "chalupa",
]
MOVITY_KW = [
    "vozidlo", "vozidla", "automobil", "osobni auto", "nakladni", "rz ", "spz ",
    "vin", "stroj", "soustruh", "freza", "technologi", "zasoby", "naradi",
    "vysokozdvizn", "prives", "naves", "traktor", "bagr", "kontejner",
    "vybaveni",
]
# "movit" je podretezec "nemovit" - matchovat jen kdyz NEnasleduje po "ne".
MOVITY_RE = re.compile(r"(?<!ne)movit")
NEHMOTNY_KW = [
    "pohledavk", "obchodni podil", "ochrann znamk", "licenc", "cenne papiry",
    "akcie", "podil ve spolecnosti", "know-how", "domena",
]

SOUPIS_KW = ["soupis majetkove podstaty", "soupisu majetkove podstaty",
             "soupise majetkove podstaty", "uveden v soupisu", "uvedeny v soupisu",
             "zapsan v soupisu", "aktualizovanem soupisu"]

CENA_KONTEXT = ["kupni cena", "za castku", "cena", "prodej za", "kupni cenu",
                "za kupni cenu", "vytezek", "za cenu"]

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_TEL_RE = re.compile(r"(?:\+?420[\s\-]?)?(?<!\d)(\d{3})[\s\-]?(\d{3})[\s\-]?(\d{3})(?!\d)")
_DS_RE = re.compile(r"(?:datov[a-z]*\s+schr[a-z]*|ID\s*DS|ID\s*datov)[^A-Za-z0-9]{0,20}([a-z0-9]{7})",
                    re.I)
_CENA_RE = re.compile(
    r"(\d{1,3}(?:[  \.]\d{3})+|\d{4,9})(?:,\-{0,2}|,\d{2})?\s*(?:K[cč]|CZK)", re.I)
_TITUL_RE = re.compile(
    r"((?:Mgr\.|JUDr\.|Ing\.|MUDr\.|Bc\.|Mgr|JUDr|Ing)[^\n,;]{2,60}"
    r"|[A-ZĚŠČŘŽÝÁÍÉÚŮŇŤĎÓ][\w\.\- ]{2,50}(?:v\.\s?o\.\s?s\.|s\.\s?r\.\s?o\.|a\.\s?s\.))")


def _strip_diacritics(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _norm(text: str) -> str:
    return _strip_diacritics(text or "").lower()


# Zkratky, za kterymi tecka NEUKONCUJE vetu (jinak se "parc. c. 694/9" roztrhne).
_ABBR = (u"parc", u"c", u"č", u"st", u"p", u"tj", u"tzv", u"resp", u"atd", u"apod",
         u"cca", u"kat", u"uz", u"uz\u00ed", u"k", u"lv", u"ul", u"nam", u"obc",
         u"odst", u"pism", u"sb", u"zak", u"ins", u"ico", u"dic", u"mgr", u"judr",
         u"ing", u"bc", u"phdr", u"mudr", u"m", u"kc", u"kč", u"vyd", u"pozem")
_ABBR_SET = set(_ABBR)


def _sentences(text: str) -> List[str]:
    """Rozdeli text na vety/radky, ale neroztrhne ceske zkratky typu 'parc. c.'."""
    out = []
    for line in re.split(r"\n+", text or ""):
        line = line.strip()
        if not line:
            continue
        buf, start = [], 0
        for m in re.finditer(r"[\.\;\:]\s+", line):
            head = line[start:m.start()]
            last = re.split(r"[\s\(\)\u2013\u2014,]", head)[-1] if head else ""
            if _strip_diacritics(last).lower() in _ABBR_SET or len(last) <= 1:
                continue  # zkratka - veta pokracuje
            nxt = line[m.end():m.end() + 1]
            if nxt and not (nxt.isupper() or nxt.isdigit() or nxt in u"-\u2013"):
                continue
            buf.append(line[start:m.end()].strip())
            start = m.end()
        buf.append(line[start:].strip())
        out.extend(b for b in buf if b)
    return [re.sub(r"\s+", " ", s).strip() for s in out if s.strip()]


def _count_hits(low: str, keywords, pattern=None) -> int:
    n = sum(1 for kw in keywords if kw in low)
    if pattern is not None:
        n += len(pattern.findall(low))
    return n


def _best_snippet(text: str, keywords: List[str], pattern=None,
                  exclude=()) -> Optional[str]:
    """Najde vetu s klicovym slovem, prednost ma veta obsahujici i cislo."""
    best, best_score = None, -1
    for sent in _sentences(text):
        low = _norm(sent)
        hits = _count_hits(low, keywords, pattern)
        if not hits:
            continue
        if sent in exclude:
            continue
        score = hits * 2
        if re.search(r"\d", sent):
            score += 3
        if 40 <= len(sent) <= 400:
            score += 2
        if score > best_score:
            best, best_score = sent, score
    if best and len(best) > 220:
        best = best[:217].rstrip() + "..."
    return best


def _parse_amount(raw: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return None
    try:
        value = int(digits)
    except ValueError:
        return None
    return value if 1000 <= value <= 5_000_000_000 else None


def _find_price(text: str) -> Optional[str]:
    """Nejvyssi vyskyt castky v okoli cenoveho klicoveho slova."""
    best = None
    for m in _CENA_RE.finditer(text or ""):
        value = _parse_amount(m.group(1))
        if value is None:
            continue
        window = _norm(text[max(0, m.start() - 160):m.end() + 60])
        near_price = any(kw in window for kw in CENA_KONTEXT)
        if not near_price:
            continue
        vat = ""
        if "vc. dph" in window or "vcetne dph" in window or "s dph" in window:
            vat = " vc. DPH"
        elif "bez dph" in window:
            vat = " bez DPH"
        if best is None or value > best[0]:
            best = (value, vat)
    if best is None:
        return None
    return "%d CZK%s" % (best[0], best[1])


def _find_contacts(text: str) -> Dict[str, Optional[str]]:
    head = (text or "")[:4000]
    email = _EMAIL_RE.search(head) or _EMAIL_RE.search(text or "")
    tel = None
    for m in _TEL_RE.finditer(head):
        candidate = "%s %s %s" % (m.group(1), m.group(2), m.group(3))
        window = _norm(head[max(0, m.start() - 60):m.start()])
        # vyhnout se IC/DIC/cislu uctu, ktere maji taky 9 cislic
        if any(bad in window for bad in ("ic:", "ico", "dic", "ucet", "c.u.", "psc")):
            continue
        tel = candidate
        break
    ds = _DS_RE.search(head)
    jmeno = None
    for line in (text or "").splitlines()[:60]:
        line = line.strip()
        if not line or len(line) > 90:
            continue
        low = _norm(line)
        if "spravce" in low or "v.o.s" in low or "insolvencni" in low:
            m = _TITUL_RE.search(line)
            jmeno = (m.group(1).strip() if m else line)
            break
    if jmeno is None:
        m = _TITUL_RE.search(head)
        jmeno = m.group(1).strip() if m else None
    return {
        "jmeno": jmeno,
        "email": email.group(0) if email else None,
        "telefon": tel,
        "datova_schranka": ds.group(1) if ds else None,
    }


def priorita_z_assets(assets: List[dict]) -> str:
    typy = set((a or {}).get("typ") for a in (assets or []))
    if "nemovity" in typy:
        return "vysoka"
    if "movity" in typy:
        return "stredni"
    return "nizka"


def needs_soupis_crossref(text: str) -> bool:
    """True kdyz navrh majetek jen odkazuje na soupis, misto aby ho popisoval."""
    low = _norm(text)
    if not any(kw in low for kw in SOUPIS_KW):
        return False
    konkretni = _count_hits(low, NEMOVITY_KW + MOVITY_KW, MOVITY_RE)
    return konkretni < 2


def heuristic_classify(text: str, case_meta: dict) -> dict:
    text = text or ""
    low = _norm(text)
    assets = []
    pouzite = []
    for typ, kws, pat in (("nemovity", NEMOVITY_KW, None),
                          ("movity", MOVITY_KW, MOVITY_RE),
                          ("nehmotny", NEHMOTNY_KW, None)):
        if _count_hits(low, kws, pat) == 0:
            continue
        popis = _best_snippet(text, kws, pat, exclude=pouzite)
        if popis is None:
            # jediny doklad uz patri jinemu typu - nevytvaret duplicitni polozku
            continue
        pouzite.append(popis)
        assets.append({"typ": typ, "popis": popis,
                       "navrhovana_cena": None, "kupujici": None})

    cena = _find_price(text)
    if cena and assets:
        assets[0]["navrhovana_cena"] = cena

    spravce = _find_contacts(text)
    priorita = priorita_z_assets(assets)

    if not assets:
        shrnuti = ("Dokument se nepodarilo automaticky rozklicovat - "
                   "otevri PDF navrhu a posud rucne.")
    else:
        labels = {"nemovity": "nemovity majetek", "movity": "movity majetek",
                  "nehmotny": "nehmotny majetek"}
        typy = ", ".join(labels[a["typ"]] for a in assets)
        shrnuti = "Spravce podal navrh na zpenezeni mimo drazbu: %s." % typy
        if cena:
            shrnuti += " Navrhovana cena %s." % cena
        shrnuti += " Klasifikovano heuristicky - overit v dokumentu."

    return {"assets": assets, "priorita": priorita, "spravce": spravce,
            "shrnuti": shrnuti, "classified_by": "heuristic"}


TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "assets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "typ": {"type": "string", "enum": list(TYPY)},
                    "popis": {"type": "string"},
                    "navrhovana_cena": {"type": ["string", "null"]},
                    "kupujici": {"type": ["string", "null"]},
                },
                "required": ["typ", "popis"],
            },
        },
        "spravce": {
            "type": "object",
            "properties": {
                "jmeno": {"type": ["string", "null"]},
                "email": {"type": ["string", "null"]},
                "telefon": {"type": ["string", "null"]},
                "datova_schranka": {"type": ["string", "null"]},
            },
        },
        "shrnuti": {"type": "string"},
    },
    "required": ["assets", "shrnuti"],
}

SYSTEM_PROMPT = (
    "Jsi asistent, ktery cte podani ceskych insolvencnich spravcu. Dostanes text "
    "'Navrhu na vydani souhlasu se zpenezenim majetkove podstaty mimo drazbu' - "
    "spravce v nem zada soud o povoleni prodat majetek dluznika mimo verejnou drazbu.\n\n"
    "Tvoje ulohy:\n"
    "1. Vypis KAZDOU majetkovou polozku, ktera je nabizena k prodeji.\n"
    "2. Kazdou zarad jako 'nemovity' (pozemky, budovy, byty, haly, arealy), "
    "'movity' (stroje, vozidla, technologie, zasoby) nebo 'nehmotny' "
    "(pohledavky, obchodni podily, ochranne znamky, licence).\n"
    "3. U kazde polozky uved navrhovanou kupni cenu a jmeno kupujiciho, POKUD "
    "jsou v textu uvedeny. Casto jeste znamy nejsou - pak vrat null.\n"
    "4. Vytahni kontakt na insolvencniho spravce (jmeno, e-mail, telefon, "
    "datova schranka) - byva v hlavicce dopisu.\n"
    "5. Napis 1-2 vety ceskeho shrnuti situace.\n\n"
    "NIKDY si nic nevymysli. Kdyz udaj v textu neni, vrat null. Popis majetku "
    "musi byt konkretni (adresa, cislo parcely, katastralni uzemi, typ stroje)."
)


def _call_llm(text: str, case_meta: dict) -> Optional[dict]:
    import anthropic  # lazy - modul se importuje i bez nainstalovaneho balicku

    client = anthropic.Anthropic()
    model = os.environ.get("ISIR_MODEL", "claude-sonnet-5")
    header = "Spisova znacka: %s\nSoud: %s\nDluznik: %s\nICO: %s\nDatum podani: %s\n\n" % (
        case_meta.get("spisova_znacka"), case_meta.get("soud"),
        case_meta.get("dluznik_jmeno"), case_meta.get("ico"),
        case_meta.get("datum_podani"),
    )
    msg = client.messages.create(
        model=model,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        tools=[{"name": "zapis_prilezitosti",
                "description": "Zapise strukturovany rozbor podani.",
                "input_schema": TOOL_SCHEMA}],
        tool_choice={"type": "tool", "name": "zapis_prilezitosti"},
        messages=[{"role": "user",
                   "content": header + "TEXT PODANI:\n" + text[:MAX_INPUT_CHARS]}],
    )
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use":
            return block.input
    return None


def classify_document(text: str, case_meta: dict) -> dict:
    """Hlavni vstupni bod. Pri jakemkoliv problemu spadne zpet na heuristiku."""
    text = text or ""
    if not text.strip():
        return heuristic_classify(text, case_meta)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return heuristic_classify(text, case_meta)

    raw = None
    for attempt in range(2):
        try:
            raw = _call_llm(text, case_meta)
            break
        except ImportError:
            log.warning("balicek 'anthropic' neni nainstalovan - heuristika")
            return heuristic_classify(text, case_meta)
        except Exception as exc:  # rate limit, overload, sit, zmena API...
            log.warning("LLM klasifikace selhala (pokus %d): %s", attempt + 1, exc)
            if attempt == 0:
                time.sleep(5)
    if not raw:
        return heuristic_classify(text, case_meta)

    assets = []
    for item in (raw.get("assets") or []):
        if not isinstance(item, dict):
            continue
        typ = item.get("typ")
        if typ not in TYPY:
            typ = "nehmotny"
        popis = (item.get("popis") or "").strip()
        if not popis:
            continue
        assets.append({
            "typ": typ,
            "popis": popis,
            "navrhovana_cena": item.get("navrhovana_cena") or None,
            "kupujici": item.get("kupujici") or None,
        })

    spravce = raw.get("spravce") if isinstance(raw.get("spravce"), dict) else {}
    fallback = _find_contacts(text)
    spravce = {k: (spravce.get(k) or fallback.get(k)) for k in
               ("jmeno", "email", "telefon", "datova_schranka")}

    return {
        "assets": assets,
        # prioritu pocitame vzdy v Pythonu, modelu ji neverime
        "priorita": priorita_z_assets(assets),
        "spravce": spravce,
        "shrnuti": (raw.get("shrnuti") or "").strip() or None,
        "classified_by": "llm",
    }


if __name__ == "__main__":
    SAMPLES = [
        ("nemovitost", u"""JUDr. Petr Novák, insolvenční správce
        e-mail: novak@aknovak.cz, tel.: +420 602 116 606, ID DS: ab3xk9q

        Navrhuji, aby soud udělil souhlas se zpeněžením mimo dražbu, a to pozemku
        parc. č. 1234/5, orná půda, o výměře 3 200 m2, zapsaného na LV č. 421 pro
        katastrální území Braník, obec Praha. Kupní cena byla sjednána ve výši
        1.985.400,- Kč vč. DPH, kupujícím je NEVEX REALITY a.s."""),
        ("pohledavka", u"""Mgr. Jana Dvořáková, v.o.s.
        e-mail: dvorakova@insolvence.cz
        Správce navrhuje zpeněžit mimo dražbu pohledávku za dlužníkem ALFA s.r.o.
        ve výši 340.000 Kč, a to za kupní cenu 45 000 Kč."""),
        ("odkaz_na_soupis", u"""Správce navrhuje zpeněžit mimo dražbu majetek zapsaný
        v aktualizovaném soupisu majetkové podstaty na č.d. 160."""),
    ]
    for name, sample in SAMPLES:
        print("=== %s ===" % name)
        print("  needs_soupis_crossref:", needs_soupis_crossref(sample))
        print(json.dumps(heuristic_classify(sample, {}), ensure_ascii=False, indent=1))
