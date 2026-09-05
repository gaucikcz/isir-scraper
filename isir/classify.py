# -*- coding: utf-8 -*-
"""Klasifikace dokumentu I_347 (navrh na zpenezeni majetkove podstaty mimo drazbu).

Jediny modul projektu, ktery utraci LLM tokeny.

Verejne API:
    classify_document(text, case_meta) -> dict
    heuristic_classify(text, case_meta) -> dict
    needs_soupis_crossref(text) -> bool

Vystupni struktura (shodna s tim, co konci v docs/data.json):
    {
      "assets": [{"typ": "nemovity"|"movity"|"nehmotny",
                  "popis": str,
                  "navrhovana_cena": str|None,
                  "kupujici": str|None}],
      "priorita": "vysoka"|"stredni"|"nizka",
      "spravce": {"jmeno": str|None, "email": str|None,
                  "telefon": str|None, "datova_schranka": str|None},
      "shrnuti": str,
      "classified_by": "llm"|"heuristic"
    }

Kompatibilita: Python 3.9+ (zadne match statementy, zadne "X | Y" anotace).
Zavislosti: pouze stdlib + volitelne balicek "anthropic".
"""

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konstanty
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-sonnet-5"
MAX_INPUT_CHARS = 24000          # posilame HEAD dokumentu (hlavicka + popis majetku)
MAX_TOKENS = 2000
RETRY_SLEEP_SECONDS = 5.0
TOOL_NAME = "zapis_prilezitosti"

ALLOWED_TYPES = ("nemovity", "movity", "nehmotny")
PRIORITY_HIGH = "vysoka"
PRIORITY_MID = "stredni"
PRIORITY_LOW = "nizka"

_TYPE_LABEL_CZ = {
    "nemovity": u"nemovitý majetek",
    "movity": u"movitý majetek",
    "nehmotny": u"nehmotný majetek",
}

# Synonyma, ktera muze vratit model, mapovana na povolene hodnoty.
_TYPE_SYNONYMS = {
    "nemovity": "nemovity",
    "nemovitost": "nemovity",
    "nemovitosti": "nemovity",
    "nemovita": "nemovity",
    "nemovite": "nemovity",
    "realestate": "nemovity",
    "real_estate": "nemovity",
    "movity": "movity",
    "movita": "movity",
    "movite": "movity",
    "movitost": "movity",
    "movitosti": "movity",
    "movable": "movity",
    "nehmotny": "nehmotny",
    "nehmotna": "nehmotny",
    "nehmotne": "nehmotny",
    "nehmotnost": "nehmotny",
    "pohledavka": "nehmotny",
    "pohledavky": "nehmotny",
    "intangible": "nehmotny",
}


# ---------------------------------------------------------------------------
# Normalizace textu (diakritika) - zachovava 1:1 delku, takze indexy do
# "folded" textu ukazuji na stejne misto v originalnim textu.
# ---------------------------------------------------------------------------

_DIA_MAP = {
    u"á": "a", u"ä": "a", u"à": "a", u"â": "a", u"ą": "a", u"ā": "a",
    u"č": "c", u"ć": "c", u"ç": "c",
    u"ď": "d", u"đ": "d",
    u"é": "e", u"ě": "e", u"ë": "e", u"ê": "e", u"ę": "e", u"è": "e",
    u"í": "i", u"î": "i", u"ï": "i", u"ì": "i",
    u"ĺ": "l", u"ľ": "l", u"ł": "l",
    u"ň": "n", u"ń": "n", u"ñ": "n",
    u"ó": "o", u"ô": "o", u"ö": "o", u"ő": "o", u"ò": "o", u"ø": "o",
    u"ř": "r", u"ŕ": "r",
    u"š": "s", u"ś": "s", u"ș": "s",
    u"ť": "t", u"ţ": "t",
    u"ú": "u", u"ů": "u", u"ü": "u", u"û": "u", u"ű": "u", u"ù": "u",
    u"ý": "y", u"ÿ": "y",
    u"ž": "z", u"ź": "z", u"ż": "z",
}


def _build_fold_table():
    # type: () -> Dict[int, str]
    table = {}  # type: Dict[int, str]
    for code in range(ord("A"), ord("Z") + 1):
        table[code] = chr(code + 32)
    for src, dst in _DIA_MAP.items():
        table[ord(src)] = dst
        upper = src.upper()
        if len(upper) == 1:
            table[ord(upper)] = dst
    # Ruzne mezery a pomlcky sjednotime, delka zustava 1:1.
    table[0x00A0] = " "
    table[0x202F] = " "
    table[0x2007] = " "
    table[0x2009] = " "
    table[0x2011] = "-"
    table[0x2013] = "-"
    table[0x2014] = "-"
    return table


_FOLD_TABLE = _build_fold_table()


def _fold(text):
    # type: (str) -> str
    """Male pismeno + bez diakritiky, delka znak po znaku zachovana."""
    if not text:
        return ""
    return text.translate(_FOLD_TABLE)


_UP = u"A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ"
_LO = u"a-záčďéěíňóřšťúůýž"


# ---------------------------------------------------------------------------
# Klicova slova / regexy (vzdy proti "folded" textu = ascii lowercase)
# ---------------------------------------------------------------------------

# (regex, strong) - "strong" znamena konkretni marker; "weak" markery
# (hole "movity"/"nemovity"/"pohledavka") jsou v ISIR bezne v pravni omacce
# ("movitych ani nemovitych veci se navrh netyka"), proto je uznavame jen
# tehdy, kdyz je ve stejne vete i prodejni kontext.
_RE_REALESTATE = [
    (r"pozemek|pozemk\w*", True),
    (r"parc\.\s*c\.|\bparcel\w*|parcelni\s*cisl\w*", True),
    (r"katastr\w*|\bk\.\s*u\.|kat\.\s*uzem\w*|katastraln\w*\s*uzem\w*", True),
    (r"list\w*\s*vlastnictv\w*|\blv\s*(?:c\.|\d)", True),
    (r"bytov\w*\s*jednotk\w*|nebytov\w*\s*(?:prostor\w*|jednotk\w*)|jednotk\w*\s*c\.\s*\d", True),
    (r"\bstavb[auy]\b|\bbudov[auy]\b|rodinn\w*\s*dum|\bhal[ayu]\b|\bareal\w*|\bgaraz\w*"
     r"|\bchat[auy]\b|\bc\.\s?p\.\s*\d", True),
    (r"orn[ae]\s*pud\w*|zastaven\w*\s*ploch\w*|travni\s*porost\w*|\bzahrad[auy]\b"
     r"|\bvymer[aey]\b", True),
    (r"spoluvlastnick\w*\s*podil\s*na\s*nemovit\w*", True),
    (r"\bnemovit\w*", False),
]

_RE_MOVABLE = [
    (r"\bvozidl\w*|\bautomobil\w*|osobni\s*(?:auto\w*|vozidl\w*)"
     r"|nakladni\s*(?:auto\w*|vozidl\w*)|\bmotocykl\w*|\bdodavk\w*", True),
    (r"\brz\s*[0-9a-z]{5,8}\b|\bspz\b|\bvin\b|registracni\s*znack\w*", True),
    (r"\bstroj\w*|soustruh\w*|\bfrez\w*|obrabec\w*|kompresor\w*|svarovac\w*|\bpil[ayu]\b", True),
    (r"technologi\w*|vyrobni\s*link\w*|\blink[ay]\s+na\b|dopravnik\w*", True),
    (r"\bzasob[ayu]\w*|skladov\w*\s*zasob\w*|zbozi\s*na\s*sklad\w*", True),
    (r"\bnarad\w*|vybaven\w*\s*(?:dilny|provozovny|kancelar\w*)|dilensk\w*", True),
    (r"vysokozdvizn\w*|\bvzv\b|paletov\w*\s*vozik\w*|manipulacni\s*technik\w*", True),
    (r"\bprives\w*|\bnaves\w*|\btraktor\w*|\bbagr\w*|nakladac\w*|rypadl\w*", True),
    (r"\bmovit\w*", False),
    (r"\bmaterial\w*", False),
]

_RE_INTANGIBLE = [
    (r"obchodni\s*podil\w*|podil\s*ve\s*spolecnost\w*|podil\s*v\s*obchodni\s*spolecnost\w*", True),
    (r"ochrann\w*\s*znamk\w*", True),
    (r"\blicenc\w*|know-how|\bpatent\w*|uzitn\w*\s*vzor\w*", True),
    (r"cenn\w*\s*papir\w*|\bakci(?:e|i|im|emi|ich)\b|dluhopis\w*|\bsmenk[ayu]\b", True),
    (r"\bpohledavk\w*|\bpohledav\w*", False),
]

_CATEGORIES = (
    ("nemovity", _RE_REALESTATE),
    ("movity", _RE_MOVABLE),
    ("nehmotny", _RE_INTANGIBLE),
)


def _compile(pairs):
    # type: (List[Tuple[str, bool]]) -> List[Tuple[Any, bool]]
    return [(re.compile(pat), strong) for pat, strong in pairs]


_COMPILED = tuple((name, _compile(pairs)) for name, pairs in _CATEGORIES)

# Prodejni kontext - podminka pro uznani "weak" markeru.
_SALE_CONTEXT_RE = re.compile(
    r"zpenez\w*|prodej\w*|prodat|kupn\w*|odkup\w*|postoup\w*|nabidk\w*|nabidl\w*"
    r"|za\s*castku|\bcen[aouy]\b|zajemc\w*|kupujic\w*|drazb\w*|soupis\w*"
)

# Odkaz na soupis majetkove podstaty misto popisu majetku primo v navrhu.
_SOUPIS_REF_RE = re.compile(
    r"soupis\w*\s*majetkov\w*\s*podstat\w*"
    r"|uveden\w*\s*v\s*soupis\w*"
    r"|zapsan\w*\s*v\s*soupis\w*"
    r"|(?:polozk\w*|pol\.)\s*(?:c\.|cislo)\s*\d"
    r"|\bc\.\s?d\.\s*\d"
)

# ---------------------------------------------------------------------------
# Cena
# ---------------------------------------------------------------------------

_AMOUNT_RE = re.compile(
    r"(?<![\d.,])"
    r"(\d{1,3}(?:[ .]\d{3})+|\d{3,12})"
    r"(?:,(?:-|\d{1,2}))?"
    r"\s*(?:,-)?\s*"
    r"(?:kc|czk|korun\w*)"
)
_AMOUNT_PREFIX_RE = re.compile(
    r"(?:kc|czk)\s*(\d{1,3}(?:[ .]\d{3})+|\d{3,12})(?:,(?:-|\d{1,2}))?"
)
# Slova, ktera musi stat tesne PRED castkou (nebo hned za ni), aby slo
# o navrhovanou kupni cenu a ne treba o vysi prihlasene pohledavky.
_PRICE_CONTEXT_RE = re.compile(
    r"kupni\s*cen\w*|\bcen[aouy]\b|\bceny\b|za\s*castku|\bcastk\w*|za\s*cenu"
    r"|prodej\w*\s*za|odkupn\w*|nabidkov\w*\s*cen\w*|nabidl\w*|nabizen\w*|uhrad\w*"
)
_PRICE_LOOKBEHIND = 70
_PRICE_LOOKAHEAD = 30
_MIN_PLAUSIBLE = 1000
_MAX_PLAUSIBLE = 100000000000

_VAT_INCL_RE = re.compile(r"vc\.\s*dph|vcetne\s*dph|s\s*dph|\+\s*dph|s\s*dani")
_VAT_EXCL_RE = re.compile(r"bez\s*dph|bez\s*dane")

# ---------------------------------------------------------------------------
# Kontakty spravce
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,10}")
_PHONE_RE = re.compile(
    r"(?<![\d/\-])((?:\+|00)\s?420[\s\-]?)?(\d{3})[\s\-]?(\d{3})[\s\-]?(\d{3})(?![\d/])"
)
_PHONE_KEYWORD_RE = re.compile(r"tel\w*|mobil\w*|\bgsm\b|\bt:\s*$|\bm:\s*$")
_DS_KEYWORD_RE = re.compile(
    r"datov\w*\s*schrank\w*|\bid\s*ds\b|\bidds\b|\bds\s*:|schrank\w*\s*id"
)
_DS_TOKEN_RE = re.compile(r"\b([a-z0-9]{7})\b")

_TITLE_NAME_RE = re.compile(
    u"\\b(?:JUDr|Mgr|Ing|MgA|Bc|PhDr|RNDr|MUDr|MVDr|doc|prof)\\.\\s*"
    u"([" + _UP + u"][" + _LO + u"]+(?:\\s+[" + _UP + u"][" + _LO + u"]+){1,2})"
)
_VOS_RE = re.compile(
    u"([" + _UP + u"][" + _UP + _LO + u"0-9&\\-\\. ]{2,45}?)\\s*,?\\s*(v\\.\\s?o\\.\\s?s\\.)"
)
_COMPANY_RE = re.compile(
    u"([" + _UP + u"][^\\n]{1,60}?"
    u"(?:a\\.\\s?s\\.|s\\.\\s?r\\.\\s?o\\.|spol\\.\\s?s\\s?r\\.\\s?o\\.|v\\.\\s?o\\.\\s?s\\.))"
)
_BUYER_KEYWORD_RE = re.compile(
    r"zajemc\w*|kupujic\w*|nabyvatel\w*|budouci\s*kupujic\w*|nabidk\w*\s*(?:od|ucinil)"
)
_ADMIN_KEYWORD_RE = re.compile(r"insolvencn\w*\s*sprav\w*|\bis\s*dluznik|spravce\s*dluznik")

# Hranice vet (nad originalnim textem, kvuli velkym pismenum). Tecka konci vetu
# jen tehdy, kdyz pred ni nestoji bezna ceska zkratka - jinak by se snippet lamal
# uprostred "parc. c. 1245/8" nebo "Mgr. Petra Novakova".
_SENT_CAND_RE = re.compile(u"(\\w+)\\.[ \\t]+(?=[" + _UP + u"])", re.UNICODE)
_PARA_RE = re.compile(r"\n[ \t]*\n")
_ABBREV = frozenset([
    "parc", "kat", "cis", "spol", "tzv", "atd", "apod", "pozn", "resp", "cca",
    "tis", "mil", "mld", "odst", "pism", "zak", "obc", "sb", "sp", "zn", "str",
    "ing", "mgr", "judr", "phdr", "rndr", "mudr", "mvdr", "mga", "prof", "doc",
    "ul", "nam", "obr", "vyd", "popr", "event", "insolv", "kop", "priloh",
])

_SNIPPET_WINDOW = 300
_SNIPPET_MAX = 220


# ---------------------------------------------------------------------------
# Pomocne funkce
# ---------------------------------------------------------------------------

def _sentence_breaks(segment):
    # type: (str) -> List[Tuple[int, int]]
    """Vrati [(pozice za teckou, pozice zacatku dalsi vety), ...]."""
    out = []  # type: List[Tuple[int, int]]
    for match in _SENT_CAND_RE.finditer(segment):
        word = _fold(match.group(1))
        if not (word.isdigit() or (len(word) >= 3 and word not in _ABBREV)):
            continue
        out.append((match.end(1) + 1, match.end()))
    return out


def _clean_snippet(raw, max_len=_SNIPPET_MAX):
    # type: (str, int) -> str
    text = re.sub(r"\s+", " ", raw or "").strip(u" \t-•·—–|:;,")
    if len(text) > max_len:
        cut = text.rfind(" ", 0, max_len)
        if cut < int(max_len * 0.6):
            cut = max_len
        text = text[:cut].rstrip(u" ,;:.-") + u"…"
    return text


def _snippet(original, start, end):
    # type: (str, int, int) -> Tuple[int, int]
    """Rozsah vety/odstavce kolem nalezu (indexy do originalniho textu)."""
    left = max(0, start - _SNIPPET_WINDOW)
    before = original[left:start]
    # O dva znaky delsi vyrez, aby regex hranice vety videl i velke pismeno,
    # ktere je prvnim znakem samotneho nalezu ("... bez DPH. |Movite veci ...").
    probe = original[left:min(len(original), start + 2)]
    cut = 0
    for match in _PARA_RE.finditer(before):
        cut = max(cut, match.end())
    for _period_end, next_start in _sentence_breaks(probe):
        if next_start <= len(before):
            cut = max(cut, next_start)
    seg_start = left + cut

    right = min(len(original), end + _SNIPPET_WINDOW)
    after = original[end:right]
    candidates = []  # type: List[int]
    para = _PARA_RE.search(after)
    if para is not None:
        candidates.append(end + para.start())
    breaks = _sentence_breaks(after)
    if breaks:
        candidates.append(end + breaks[0][0])
    seg_end = min(candidates) if candidates else right
    return seg_start, seg_end


def _plural_polozky(count):
    # type: (int) -> str
    if count == 1:
        return u"1 položka"
    if 2 <= count <= 4:
        return u"%d položky" % count
    return u"%d položek" % count


def _priority_from_assets(assets):
    # type: (List[Dict[str, Any]]) -> str
    types = set()
    for asset in assets or []:
        if isinstance(asset, dict):
            types.add(asset.get("typ"))
    if "nemovity" in types:
        return PRIORITY_HIGH
    if "movity" in types:
        return PRIORITY_MID
    return PRIORITY_LOW


def _empty_spravce():
    # type: () -> Dict[str, Optional[str]]
    return {"jmeno": None, "email": None, "telefon": None, "datova_schranka": None}


def _as_optional_str(value, max_len=300):
    # type: (Any, int) -> Optional[str]
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = u"%d" % int(value)
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return None
    value = re.sub(r"\s+", " ", value).strip()
    if not value or _fold(value) in ("null", "none", "n/a", "neuvedeno", "-", "neni uvedeno"):
        return None
    return value[:max_len]


# ---------------------------------------------------------------------------
# Detekce ceny
# ---------------------------------------------------------------------------

def _parse_amount(raw):
    # type: (str) -> Optional[int]
    digits = raw.replace(" ", "").replace(".", "")
    if not digits.isdigit():
        return None
    value = int(digits)
    if value < _MIN_PLAUSIBLE or value > _MAX_PLAUSIBLE:
        return None
    return value


def _vat_suffix(folded, start, end):
    # type: (str, int, int) -> str
    window = folded[max(0, start - 60):min(len(folded), end + 60)]
    if _VAT_EXCL_RE.search(window):
        return u" bez DPH"
    if _VAT_INCL_RE.search(window):
        return u" vč. DPH"
    return u""


def _find_price(folded):
    # type: (str) -> Tuple[Optional[str], Optional[int]]
    """Nejvyssi verohodna castka, ktera stoji u cenoveho klicoveho slova."""
    near = []  # type: List[Tuple[int, int, int]]
    fallback = []  # type: List[Tuple[int, int, int]]
    for regex in (_AMOUNT_RE, _AMOUNT_PREFIX_RE):
        for match in regex.finditer(folded):
            value = _parse_amount(match.group(1))
            if value is None:
                continue
            before = folded[max(0, match.start() - _PRICE_LOOKBEHIND):match.start()]
            after = folded[match.end():match.end() + _PRICE_LOOKAHEAD]
            entry = (value, match.start(), match.end())
            if _PRICE_CONTEXT_RE.search(before) or _PRICE_CONTEXT_RE.search(after):
                near.append(entry)
            else:
                fallback.append(entry)
    pool = near or fallback
    if not pool:
        return None, None
    pool.sort(key=lambda item: (-item[0], item[1]))
    value, start, end = pool[0]
    return u"%d CZK%s" % (value, _vat_suffix(folded, start, end)), start


# ---------------------------------------------------------------------------
# Detekce kontaktu spravce
# ---------------------------------------------------------------------------

def _find_email(text):
    # type: (str) -> Optional[str]
    for match in _EMAIL_RE.finditer(text or ""):
        candidate = match.group(0).strip(".,;)")
        if _fold(candidate).endswith((".png", ".jpg", ".gif")):
            continue
        return candidate
    return None


def _find_phone(text, folded):
    # type: (str, str) -> Optional[str]
    for match in _PHONE_RE.finditer(text or ""):
        prefix = match.group(1)
        before = folded[max(0, match.start() - 30):match.start()]
        after = folded[match.end():match.end() + 6]
        if re.match(r"\s*(?:kc|czk|m2|ks\b)", after):
            continue
        if re.search(r"ic[o:]|dic|ucet|c\.\s?u\.|psc|\bcislo\b", before):
            continue
        if not prefix and not _PHONE_KEYWORD_RE.search(before):
            continue
        number = u"%s %s %s" % (match.group(2), match.group(3), match.group(4))
        return (u"+420 " + number) if prefix else number
    return None


def _find_datova_schranka(folded):
    # type: (str) -> Optional[str]
    for keyword in _DS_KEYWORD_RE.finditer(folded):
        window = folded[keyword.end():keyword.end() + 70]
        letters_only = None
        for token in _DS_TOKEN_RE.finditer(window):
            value = token.group(1)
            if value in ("schrank", "datovka", "spravce"):
                continue
            if any(char.isdigit() for char in value):
                return value
            if letters_only is None:
                letters_only = value
        if letters_only:
            return letters_only
    return None


def _find_spravce_jmeno(text, folded):
    # type: (str, str) -> Optional[str]
    head = (text or "")[:4000]
    head_folded = folded[:4000]
    anchors = [m.start() for m in _ADMIN_KEYWORD_RE.finditer(head_folded)]
    names = [(m.start(), m.group(0).strip()) for m in _TITLE_NAME_RE.finditer(head)]
    if names:
        if anchors:
            names.sort(key=lambda item: min(abs(item[0] - a) for a in anchors))
        return re.sub(r"\s+", " ", names[0][1]).strip(u" ,;")
    match = _VOS_RE.search(head)
    if match:
        return re.sub(r"\s+", " ", u"%s, %s" % (match.group(1).strip(u" ,"), match.group(2)))
    return None


def _find_buyer(text, folded, admin_name):
    # type: (str, str, Optional[str]) -> Optional[str]
    for keyword in _BUYER_KEYWORD_RE.finditer(folded):
        window_start = keyword.end()
        match = _COMPANY_RE.search(text[window_start:window_start + 160])
        if not match:
            continue
        candidate = re.sub(r"\s+", " ", match.group(1)).strip(u" ,;")
        if len(candidate) < 4:
            continue
        if admin_name and _fold(candidate) in _fold(admin_name):
            continue
        return candidate
    return None


def _find_contacts(text, folded):
    # type: (str, str) -> Dict[str, Optional[str]]
    return {
        "jmeno": _find_spravce_jmeno(text, folded),
        "email": _find_email(text),
        "telefon": _find_phone(text, folded),
        "datova_schranka": _find_datova_schranka(folded),
    }


# ---------------------------------------------------------------------------
# Heuristicka klasifikace
# ---------------------------------------------------------------------------

def _score_candidate(snippet_folded, strong):
    # type: (str, bool) -> int
    score = 3 if strong else 0
    if re.search(r"\d", snippet_folded):
        score += 3
    if re.search(r"\bkc\b|czk", snippet_folded):
        score += 2
    if _SALE_CONTEXT_RE.search(snippet_folded):
        score += 1
    if 50 <= len(snippet_folded) <= _SNIPPET_MAX:
        score += 1
    return score


def _detect_assets(text, folded):
    # type: (str, str) -> List[Dict[str, Any]]
    """Pro kazdou detekovanou kategorii vytvori jednu polozku majetku."""
    assets = []  # type: List[Dict[str, Any]]
    for typ, patterns in _COMPILED:
        best = None  # type: Optional[Tuple[int, int, int, str]]
        for regex, strong in patterns:
            for match in regex.finditer(folded):
                seg_start, seg_end = _snippet(text, match.start(), match.end())
                raw = text[seg_start:seg_end]
                snippet_folded = _fold(raw)
                if not strong and not _SALE_CONTEXT_RE.search(snippet_folded):
                    continue
                score = _score_candidate(snippet_folded, strong)
                if best is None or score > best[0] or (score == best[0] and seg_start < best[1]):
                    best = (score, seg_start, seg_end, raw)
        if best is not None:
            popis = _clean_snippet(best[3])
            if popis:
                assets.append({
                    "typ": typ,
                    "popis": popis,
                    "navrhovana_cena": None,
                    "kupujici": None,
                    "_pos": (best[1] + best[2]) // 2,
                })
    return assets


def _build_shrnuti(assets, price, buyer, case_meta, soupis_only):
    # type: (List[Dict[str, Any]], Optional[str], Optional[str], Dict[str, Any], bool) -> str
    dluznik = _as_optional_str((case_meta or {}).get("dluznik_jmeno"), 120)
    subject = u"Insolvenční správce podal návrh na zpeněžení majetkové podstaty mimo dražbu"
    if dluznik:
        subject += u" (dlužník: %s)" % dluznik

    if not assets:
        parts = [subject + u"."]
        if soupis_only:
            parts.append(u"Majetek není v návrhu popsán, je pouze odkázán do soupisu "
                         u"majetkové podstaty.")
        else:
            parts.append(u"Konkrétní položky majetku se z textu nepodařilo automaticky rozpoznat.")
        parts.append(u"Klasifikováno heuristicky – nutná ruční kontrola dokumentu.")
        return u" ".join(parts)

    labels = u", ".join(_TYPE_LABEL_CZ[asset["typ"]] for asset in assets)
    parts = [u"%s: %s (%s)." % (subject, labels, _plural_polozky(len(assets)))]
    if price:
        parts.append(u"Navrhovaná cena %s." % price)
    if buyer:
        parts.append(u"Kupující: %s" % (buyer if buyer.endswith(u".") else buyer + u"."))
    if soupis_only:
        parts.append(u"Podrobnosti jsou v soupisu majetkové podstaty.")
    parts.append(u"Klasifikováno heuristicky – ověřit v dokumentu.")
    return u" ".join(parts)


def heuristic_classify(text, case_meta):
    # type: (str, Dict[str, Any]) -> Dict[str, Any]
    """Ciste regexova klasifikace bez site. Slouzi i jako fallback pro LLM."""
    case_meta = case_meta or {}
    text = text or ""
    folded = _fold(text)

    assets = _detect_assets(text, folded)
    price, price_pos = _find_price(folded)
    contacts = _find_contacts(text, folded)
    buyer = _find_buyer(text, folded, contacts.get("jmeno"))

    if assets and price:
        if price_pos is None:
            target = assets[0]
        else:
            target = min(assets, key=lambda a: abs(a["_pos"] - price_pos))
        target["navrhovana_cena"] = price
    if assets and buyer:
        assets[0]["kupujici"] = buyer

    soupis_only = needs_soupis_crossref(text)
    shrnuti = _build_shrnuti(assets, price, buyer, case_meta, soupis_only)

    for asset in assets:
        asset.pop("_pos", None)

    return {
        "assets": assets,
        "priorita": _priority_from_assets(assets),
        "spravce": contacts,
        "shrnuti": shrnuti,
        "classified_by": "heuristic",
    }


# ---------------------------------------------------------------------------
# Krizovy odkaz na soupis majetkove podstaty
# ---------------------------------------------------------------------------

def needs_soupis_crossref(text):
    # type: (str) -> bool
    """True, kdyz navrh majetek jen odkazuje do soupisu misto konkretniho popisu.

    Pipeline pak dotahne nejblizsi predchozi dokument "Soupis majetkove podstaty".
    """
    if not text:
        return False
    folded = _fold(text)
    if not _SOUPIS_REF_RE.search(folded):
        return False
    for _typ, patterns in _COMPILED[:2]:  # nemovity + movity
        for regex, strong in patterns:
            if strong and regex.search(folded):
                return False
    return True


# ---------------------------------------------------------------------------
# LLM klasifikace
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = u"""Jsi analytik českého insolvenčního rejstříku (ISIR) zaměřený na distressed aktiva.

Dostaneš text dokumentu typu "Návrh na vydání souhlasu se zpeněžením majetkové podstaty mimo dražbu"
(událost I_347). Jde o podání insolvenčního správce, kterým žádá insolvenční soud o souhlas s prodejem
majetku z majetkové podstaty dlužníka MIMO veřejnou dražbu. Text pochází z pdftotext nebo OCR, může být
zalomený, neúplný nebo obsahovat překlepy.

Tvůj úkol:
1. Najdi VŠECHNY položky majetku, které jsou nabízeny k prodeji, a každou zapiš jako samostatný záznam.
   Nezapisuj majetek, který je zmíněn jen v právní citaci nebo o kterém se výslovně píše, že se neprodává.
2. Každou položku zařaď do jedné ze tří kategorií:
   - "nemovity"  = nemovitosti: pozemky, parcely, budovy, byty a nebytové jednotky, haly, areály,
                   garáže, spoluvlastnické podíly na nemovitostech, stavby zapsané v katastru.
   - "movity"    = movité věci: stroje, technologie, výrobní linky, vozidla, přívěsy, nářadí,
                   vysokozdvižné vozíky, zásoby, zboží, vybavení provozovny.
   - "nehmotny"  = nehmotný majetek a práva: pohledávky, obchodní podíly, akcie a cenné papíry,
                   ochranné známky, licence, know-how, patenty.
3. Do "popis" napiš krátký a konkrétní popis položky česky: adresa, katastrální území, parcelní číslo,
   číslo LV, výměra, typ a značka stroje, dlužník pohledávky apod. Žádná obecná slova typu "majetek".
4. "navrhovana_cena": navrhovaná kupní cena právě této položky, pokud ji text uvádí. Formát
   "1985400 CZK", případně "1985400 CZK vč. DPH" nebo "1985400 CZK bez DPH". Jinak null.
   Nezaměňuj kupní cenu s nominální výší pohledávky nebo s odhadní cenou ze znaleckého posudku.
5. "kupujici": jméno nebo firma konkrétního zájemce/kupujícího, pokud je v textu uvedeno. Jinak null.
6. "spravce": kontakt na insolvenčního správce, obvykle v hlavičce dokumentu - jméno (včetně titulu
   nebo názvu v.o.s.), e-mail, telefon, ID datové schránky (7 znaků). Co v textu není, nech null.
7. "shrnuti": 1-2 věty česky, co se prodává a za kolik. Věcně, bez marketingu.

DŮLEŽITÉ: Nikdy si nic nevymýšlej a nedopočítávej. Co v dokumentu není, zapiš jako null. Raději méně
údajů než nepřesné údaje. Výsledek vždy zapiš voláním nástroje zapis_prilezitosti."""

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": u"Zapíše strukturovaný záznam o příležitosti (majetek nabízený k prodeji mimo dražbu).",
    "input_schema": {
        "type": "object",
        "properties": {
            "assets": {
                "type": "array",
                "description": u"Seznam všech položek majetku nabízených k prodeji.",
                "items": {
                    "type": "object",
                    "properties": {
                        "typ": {
                            "type": "string",
                            "enum": list(ALLOWED_TYPES),
                            "description": u"Kategorie majetku.",
                        },
                        "popis": {
                            "type": "string",
                            "description": u"Konkrétní popis položky česky.",
                        },
                        "navrhovana_cena": {
                            "type": ["string", "null"],
                            "description": u"Např. '1985400 CZK vč. DPH', jinak null.",
                        },
                        "kupujici": {
                            "type": ["string", "null"],
                            "description": u"Jméno nebo firma zájemce, jinak null.",
                        },
                    },
                    "required": ["typ", "popis"],
                },
            },
            "priorita": {
                "type": "string",
                "enum": [PRIORITY_HIGH, PRIORITY_MID, PRIORITY_LOW],
                "description": u"Orientační priorita (klient si ji stejně přepočítá sám).",
            },
            "spravce": {
                "type": "object",
                "description": u"Kontakt na insolvenčního správce z hlavičky dokumentu.",
                "properties": {
                    "jmeno": {"type": ["string", "null"]},
                    "email": {"type": ["string", "null"]},
                    "telefon": {"type": ["string", "null"]},
                    "datova_schranka": {"type": ["string", "null"]},
                },
            },
            "shrnuti": {
                "type": "string",
                "description": u"1-2 věty česky.",
            },
        },
        "required": ["assets", "shrnuti"],
    },
}


def _build_user_message(text, case_meta):
    # type: (str, Dict[str, Any]) -> str
    meta = case_meta or {}
    lines = [u"Metadata případu z ISIR:"]
    for label, key in (
        (u"spisová značka", "spisova_znacka"),
        (u"soud", "soud"),
        (u"dlužník", "dluznik_jmeno"),
        (u"IČO", "ico"),
        (u"datum podání", "datum_podani"),
    ):
        value = meta.get(key)
        if value is None or value == "":
            value = u"neuvedeno"
        lines.append(u"- %s: %s" % (label, value))
    body = text or ""
    truncated = len(body) > MAX_INPUT_CHARS
    body = body[:MAX_INPUT_CHARS]
    lines.append(u"")
    lines.append(u"--- TEXT DOKUMENTU%s ---" % (u" (zkrácen)" if truncated else u""))
    lines.append(body)
    lines.append(u"--- KONEC TEXTU ---")
    lines.append(u"")
    lines.append(u"Zapiš výsledek voláním nástroje %s." % TOOL_NAME)
    return u"\n".join(lines)


def _is_retryable(message):
    # type: (str) -> bool
    low = message.lower()
    return any(token in low for token in (
        "rate limit", "rate_limit", "429", "overload", "529", "timeout",
        "timed out", "connection", "temporarily", "503", "502", "500",
    ))


def _call_api(client, model, system_prompt, user_message):
    # type: (Any, str, str, str) -> Any
    """Jedno volani API s jednim retry na rate-limit/overload."""
    kwargs = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system_prompt,
        "tools": [TOOL_SCHEMA],
        "tool_choice": {"type": "tool", "name": TOOL_NAME},
        "messages": [{"role": "user", "content": user_message}],
        # Jednoducha extrakce z jednoho dokumentu: bez thinkingu se cely rozpocet
        # max_tokens vejde do volani nastroje a davkove zpracovani je levnejsi.
        "thinking": {"type": "disabled"},
    }
    retried = False
    while True:
        try:
            return client.messages.create(**kwargs)
        except Exception as exc:  # siroky zamer je zde zamerny
            message = str(exc)
            if "thinking" in message.lower() and "thinking" in kwargs:
                # Nektere modely thinking vypnout nedovoli - zkusime to bez nej.
                kwargs.pop("thinking")
                continue
            if not retried and _is_retryable(message):
                retried = True
                LOG.warning("LLM klasifikace: docasna chyba (%s), retry za %s s",
                            message[:200], RETRY_SLEEP_SECONDS)
                time.sleep(RETRY_SLEEP_SECONDS)
                continue
            raise


def _extract_tool_payload(response):
    # type: (Any) -> Optional[Dict[str, Any]]
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) != "tool_use":
            continue
        if getattr(block, "name", None) != TOOL_NAME:
            continue
        payload = getattr(block, "input", None)
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, str):
            try:
                parsed = json.loads(payload)
            except ValueError:
                return None
            if isinstance(parsed, dict):
                return parsed
    return None


def _coerce_typ(value):
    # type: (Any) -> Optional[str]
    if not isinstance(value, str):
        return None
    key = _fold(value).strip().replace(" ", "").replace("-", "_")
    if key in ALLOWED_TYPES:
        return key
    if key in _TYPE_SYNONYMS:
        return _TYPE_SYNONYMS[key]
    for prefix, target in (("nemovit", "nemovity"), ("nehmotn", "nehmotny"), ("movit", "movity")):
        if key.startswith(prefix):
            return target
    return None


def _normalise_llm_payload(payload, text, folded, case_meta):
    # type: (Dict[str, Any], str, str, Dict[str, Any]) -> Dict[str, Any]
    assets = []  # type: List[Dict[str, Any]]
    raw_assets = payload.get("assets")
    if isinstance(raw_assets, list):
        for item in raw_assets:
            if not isinstance(item, dict):
                continue
            typ = _coerce_typ(item.get("typ"))
            popis = _as_optional_str(item.get("popis"), 400)
            if typ is None:
                LOG.warning("LLM klasifikace: neplatny typ majetku %r, polozka vynechana",
                            item.get("typ"))
                continue
            if not popis:
                continue
            assets.append({
                "typ": typ,
                "popis": popis,
                "navrhovana_cena": _as_optional_str(item.get("navrhovana_cena"), 120),
                "kupujici": _as_optional_str(item.get("kupujici"), 200),
            })

    spravce = _empty_spravce()
    raw_spravce = payload.get("spravce")
    if isinstance(raw_spravce, dict):
        for key in spravce:
            spravce[key] = _as_optional_str(raw_spravce.get(key), 200)
    # Co model prehledl, doplnime z regexu nad hlavickou dokumentu.
    fallback_contacts = _find_contacts(text, folded)
    for key in spravce:
        if not spravce[key]:
            spravce[key] = fallback_contacts.get(key)

    shrnuti = _as_optional_str(payload.get("shrnuti"), 800)
    if not shrnuti:
        shrnuti = _build_shrnuti(assets, None, None, case_meta, False)

    return {
        "assets": assets,
        # Prioritu pocitame VZDY v Pythonu, hodnote od modelu neverime.
        "priorita": _priority_from_assets(assets),
        "spravce": spravce,
        "shrnuti": shrnuti,
        "classified_by": "llm",
    }


def classify_document(text, case_meta):
    # type: (str, Dict[str, Any]) -> Dict[str, Any]
    """Hlavni vstupni bod. LLM pokud je k dispozici, jinak (a pri chybe) heuristika."""
    case_meta = case_meta or {}
    text = text or ""

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not text.strip():
        return heuristic_classify(text, case_meta)

    try:
        import anthropic
    except Exception as exc:  # ImportError i chyby pri importu
        LOG.warning("Balicek 'anthropic' neni k dispozici (%s), pouzivam heuristiku", exc)
        return heuristic_classify(text, case_meta)

    model = os.environ.get("ISIR_MODEL") or DEFAULT_MODEL
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = _call_api(client, model, SYSTEM_PROMPT, _build_user_message(text, case_meta))
        payload = _extract_tool_payload(response)
        if payload is None:
            raise ValueError("odpoved neobsahuje blok tool_use '%s'" % TOOL_NAME)
        result = _normalise_llm_payload(payload, text, _fold(text), case_meta)
    except Exception as exc:  # jakakoliv chyba = fallback na heuristiku
        LOG.warning("LLM klasifikace selhala (%s: %s), pouzivam heuristiku",
                    type(exc).__name__, str(exc)[:300])
        return heuristic_classify(text, case_meta)

    if not result["assets"]:
        fallback = heuristic_classify(text, case_meta)
        if fallback["assets"]:
            LOG.warning("LLM nenasel zadny majetek, pouzivam heuristicky vysledek")
            return fallback
    return result


# ---------------------------------------------------------------------------
# Smoke test:  python isir/classify.py
# ---------------------------------------------------------------------------

_SAMPLE_REALESTATE = u"""Mgr. Petra Nováková, insolvenční správce, IČO 71234567
sídlo: Sokolská 1795/56, 120 00 Praha 2
tel.: +420 725 118 902, e-mail: podatelna@ak-novakova.cz, ID datové schránky: 7xk3fqz

Krajský soud v Ústí nad Labem
sp. zn. KSUL 89 INS 12345/2024

Návrh na vydání souhlasu se zpeněžením majetkové podstaty mimo dražbu

Insolvenční správce navrhuje zpeněžit mimo dražbu nemovité věci zapsané v soupisu majetkové
podstaty pod položkou č. 3, a to pozemek parc. č. 1245/8, orná půda, o výměře 3 480 m2, a budovu
č.p. 214 na pozemku parc. č. 1245/9, vše zapsané na listu vlastnictví č. 1102 pro katastrální
území Chomutov, obec Chomutov, u Katastrálního úřadu pro Ústecký kraj.

Zájemce NEVEX REALITY a.s., IČO 27845123, nabídl kupní cenu ve výši 1.985.400,- Kč vč. DPH,
což je nejvyšší z doručených nabídek. Správce proto navrhuje, aby soud vydal souhlas.
"""

_SAMPLE_RECEIVABLE = u"""JUDr. Tomáš Bárta, insolvenční správce
Bártova a partneři, v.o.s., Masarykova 12, 602 00 Brno
tel. 542 210 118, e-mail: spravce@bartaops.cz, datová schránka: qw8m2rp

Návrh na udělení souhlasu se zpeněžením mimo dražbu

Předmětem zpeněžení je pohledávka dlužníka za společností STAVOKOMPLET spol. s r.o. ve výši
742 000 Kč, zapsaná v soupisu majetkové podstaty pod pol. č. 12. Zájemce nabídl za postoupení
pohledávky částku 250.000 Kč bez DPH. Movité ani nemovité věci nejsou předmětem tohoto návrhu.
"""


def _selftest():
    # type: () -> None
    samples = (
        (u"VZOREK 1 - nemovitosti", _SAMPLE_REALESTATE,
         {"spisova_znacka": "KSUL 89 INS 12345/2024", "soud": u"KS Ústí nad Labem",
          "dluznik_jmeno": u"ABC Stavby s.r.o.", "ico": "25478963",
          "datum_podani": "2026-09-01"}),
        (u"VZOREK 2 - pohledávka", _SAMPLE_RECEIVABLE,
         {"spisova_znacka": "KSBR 24 INS 998/2025", "soud": u"KS Brno",
          "dluznik_jmeno": u"Jan Dvořák", "ico": None, "datum_podani": "2026-09-02"}),
    )
    for title, text, meta in samples:
        print(u"=" * 78)
        print(u"%s   |   needs_soupis_crossref: %s" % (title, needs_soupis_crossref(text)))
        print(u"=" * 78)
        print(json.dumps(heuristic_classify(text, meta), ensure_ascii=False, indent=2))
        print(u"")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    _selftest()
