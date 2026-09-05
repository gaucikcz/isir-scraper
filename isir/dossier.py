# -*- coding: utf-8 -*-
"""Dossier k jedne veci: syrovy text vsech relevantnich dokumentu v jednom .md.

ZADNE LLM TOKENY. Tenhle modul NEVOLA zadny jazykovy model a ani ho volat
nema - jen stahne dokumenty z ISIR, prozene je pres pdftotext (s OCR
fallbackem, viz isir/extract.py) a slozi jeden markdown soubor. Cely smysl je,
ze si vysledek vlozi uzivatel do LLM sam, kdyz uzna za vhodne - klasifikace v
pipeline uz jednou probehla a znovu ji platit netreba.

Proc dossier a ne odkaz na jeden dokument: mereni na 20 vecech s vysokou
prioritou ukazalo, ze zadny JEDEN typ dokumentu neni spolehlive pritomny
(usneseni o prodeji mimo drazbu 50 %, soupis majetkove podstaty 30 %, vypis
z katastru 25 %, zprava spravce 25 %, znalecky posudek 0 %). Slozit dohromady
to, co u veci opravdu je, je proto jedina cesta, jak dostat kompletni obraz.
"""
import json
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from . import config, db, detail, extract, pipeline

log = logging.getLogger(__name__)

# Podadresar v docs/ - dashboard je servirovan z docs/, takze odkaz v data.json
# je relativni "dossier/<slug>.md".
DOSSIER_SUBDIR = "dossier"

# Stropy. Musi byt v hlavicce vysledku, aby zkraceny dossier NIKDY nevypadal
# jako uplny - jinak by uzivatel usoudil, ze u veci nic dalsiho neni.
MAX_DOCS = 8
MAX_CHARS = 350000

_DOCID_RE = re.compile(r"[?&]id=(\d+)")
_NONALNUM_RE = re.compile(r"[^a-z0-9]+")

_KATEGORIE_CZ = {
    "navrh": u"návrh na zpeněžení mimo dražbu",
    "soupis": u"soupis majetkové podstaty",
    "katastr": u"katastr nemovitostí",
    "zprava": u"zpráva správce",
    "prodej": u"POZOR: schválený prodej",
}


def slugify(spisova_znacka: str) -> str:
    """'KSOS 37 INS 16018/2019' -> 'ksos-37-ins-16018-2019'.

    Jmeno souboru dossieru. Pouziva ho i pipeline.export_json, aby odkaz
    v data.json ukazoval presne na soubor, ktery vznikl.
    """
    text = unicodedata.normalize("NFKD", spisova_znacka or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _NONALNUM_RE.sub("-", text.lower()).strip("-")


def _norm(spisova_znacka: str) -> str:
    """Porovnavaci tvar znacky - bez mezer, lomitek a diakritiky, velkymi."""
    return slugify(spisova_znacka).replace("-", "").upper()


def _find_case(conn, spisova_znacka: str) -> dict:
    """Najde vec v databazi. Znacku bere i bez mezer / s jinym oddelovacem."""
    zadani = (spisova_znacka or "").strip()
    if not zadani:
        raise ValueError(u"Nezadal jsi spisovou značku.")

    row = conn.execute("SELECT * FROM cases WHERE spisova_znacka = ?", (zadani,)).fetchone()
    if row is not None:
        return dict(row)

    hledane = _norm(zadani)
    for candidate in conn.execute("SELECT * FROM cases").fetchall():
        if _norm(candidate["spisova_znacka"]) == hledane:
            return dict(candidate)

    raise ValueError(
        u"Věc %s není v databázi (data/isir.sqlite3). Zkontroluj spisovou značku, "
        u"nebo ji nejdřív posbírej během `cli.py daily` / `cli.py backfill`."
        % zadani
    )


def _case_opportunities(conn, spisova_znacka: str) -> List[dict]:
    rows = conn.execute(
        """
        SELECT o.priorita, o.assets_json, o.shrnuti, o.classified_by, o.status,
               o.spravce_jmeno, o.spravce_email, o.spravce_telefon,
               o.spravce_datova_schranka,
               e.event_date, e.event_label, e.doc_url_main
        FROM opportunities o
        JOIN events e ON e.doc_id = o.doc_id
        WHERE e.spisova_znacka = ?
        ORDER BY e.event_date DESC, o.id DESC
        """,
        (spisova_znacka,),
    ).fetchall()
    return [dict(r) for r in rows]


def _store_case_docs(spisova_znacka: str, soupis: Optional[dict], dokumenty: List[dict]) -> None:
    """Cerstvy detail veci si rovnou ulozime - stahli jsme ho tak jako tak.

    Dashboard tak po sestaveni dossieru ukazuje stejny seznam dokumentu, jaky
    je v dossieru. Selhani zapisu nesmi shodit dossier - ten uz je hotovy.
    """
    try:
        conn = db.connect()
        db.init_schema(conn)
        try:
            sets = ["dokumenty_json=?"]
            params = [json.dumps(dokumenty, ensure_ascii=False)]
            if soupis:
                sets += ["soupis_doc_id=?", "soupis_date=?", "soupis_label=?"]
                params += [soupis["doc_id"],
                           soupis["datum"].isoformat() if soupis["datum"] else None,
                           soupis.get("popis")]
            params.append(spisova_znacka)
            conn.execute("UPDATE cases SET %s WHERE spisova_znacka=?" % ", ".join(sets),
                         params)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Ulozeni dokumentu k veci %s selhalo: %s", spisova_znacka, exc)


def _doc_id_from_url(url: str) -> Optional[str]:
    m = _DOCID_RE.search(url or "")
    return m.group(1) if m else None


def _fence(text: str) -> str:
    """Ohradka delsi nez nejdelsi souvisly beh backticku uvnitr textu."""
    longest = max([len(run) for run in re.findall(r"`+", text or "")] or [0])
    return "`" * max(3, longest + 1)


def _cislo(value: int) -> str:
    """1234567 -> '1 234 567' (nezalomitelna mezera by v .md jen prekazela)."""
    return "{:,}".format(int(value)).replace(",", " ")


def _hlavicka(case: dict, dokumenty: List[dict], now: str) -> List[str]:
    znacka = case.get("spisova_znacka") or "?"
    lines = [
        u"# Dossier: %s" % znacka,
        u"",
        u"Sestaveno %s nastrojem isir-scraper. Obsah je POUZE syrovy text stažený "
        u"z ISIR (pdftotext, u skenů OCR) - žádný jazykový model tento soubor "
        u"nečetl ani nepsal. Je to podklad k vložení do LLM, ne hotová analýza." % now,
        u"",
        u"## Věc",
        u"",
        u"| Pole | Hodnota |",
        u"| --- | --- |",
    ]
    for label, value in (
        (u"Dlužník", case.get("dluznik_jmeno")),
        (u"IČO", case.get("ico")),
        (u"Spisová značka", znacka),
        (u"Soud", case.get("soud")),
        (u"Stav řízení", case.get("stav_rizeni")),
        (u"Sídlo / bydliště", case.get("sidlo")),
    ):
        lines.append(u"| %s | %s |" % (label, _cell(value)))
    if case.get("detail_url"):
        lines.append(u"| Detail v ISIR | %s |" % case["detail_url"])
    if case.get("or_url"):
        lines.append(u"| Obchodní rejstřík | %s |" % case["or_url"])

    prodeje = [d for d in dokumenty if d.get("kategorie") == "prodej"]
    if prodeje:
        lines += [
            u"",
            u"> **VAROVÁNÍ:** v této věci už soud nějaký prodej schválil "
            u"(%d dokument/ů kategorie „prodej“ níže). Příležitost může být pryč - "
            u"přečti si je dřív, než správci zavoláš." % len(prodeje),
        ]
    return lines


def _cell(value) -> str:
    """Hodnota do tabulky - prazdna se pozna, svisitko by rozbilo radek."""
    text = (value if value is not None else u"").strip() if isinstance(value, str) else value
    if not text:
        return u"-"
    return str(text).replace("|", "\\|").replace("\n", " ")


def _limity(pouzito: int, celkem: int, znaku: int, max_docs: int, max_chars: int,
            zkraceno: bool) -> List[str]:
    lines = [
        u"",
        u"## Rozsah a limity",
        u"",
        u"- Dokumentů v dossieru: **%d** z **%d** nalezených (strop **%d**)."
        % (pouzito, celkem, max_docs),
        u"- Extrahovaného textu: **%s** znaků (strop **%s**)."
        % (_cislo(znaku), _cislo(max_chars)),
    ]
    if pouzito < celkem or zkraceno:
        lines.append(
            u"- **Tento dossier NENÍ úplný** - část dokumentů se do stropů nevešla. "
            u"Seznam vynechaných je na konci souboru, otevři si je ručně."
        )
    else:
        lines.append(u"- Do stropů se vešlo všechno, co shortlist u věci našel.")
    return lines


def _prilezitosti(rows: List[dict]) -> List[str]:
    lines = [u"", u"## Vyhodnocené příležitosti (z databáze scraperu)", u""]
    if not rows:
        lines.append(u"K této věci není v databázi žádná klasifikovaná příležitost "
                     u"(návrh na zpeněžení mimo dražbu se ještě nezpracoval).")
        return lines

    for row in rows:
        lines.append(u"### Návrh z %s - priorita %s (%s)"
                     % (row.get("event_date") or u"?",
                        row.get("priorita") or u"?",
                        row.get("classified_by") or u"neznámý zdroj"))
        lines.append(u"")
        if row.get("shrnuti"):
            lines += [u"%s" % row["shrnuti"], u""]
        try:
            assets = json.loads(row.get("assets_json") or "[]")
        except (ValueError, TypeError):
            assets = []
        if assets:
            lines.append(u"**Majetek:**")
            lines.append(u"")
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                detaily = []
                if asset.get("navrhovana_cena"):
                    detaily.append(u"cena %s" % asset["navrhovana_cena"])
                if asset.get("kupujici"):
                    detaily.append(u"kupující %s" % asset["kupujici"])
                lines.append(u"- (%s) %s%s" % (
                    asset.get("typ") or u"?",
                    asset.get("popis") or u"?",
                    (u" - " + u", ".join(detaily)) if detaily else u"",
                ))
            lines.append(u"")
        kontakty = [
            (u"jméno", row.get("spravce_jmeno")),
            (u"e-mail", row.get("spravce_email")),
            (u"telefon", row.get("spravce_telefon")),
            (u"datová schránka", row.get("spravce_datova_schranka")),
        ]
        kontakty = [(k, v) for k, v in kontakty if v]
        if kontakty:
            lines.append(u"**Insolvenční správce:** "
                         + u", ".join(u"%s %s" % (k, v) for k, v in kontakty))
            lines.append(u"")
        if row.get("doc_url_main"):
            lines.append(u"Zdrojový dokument: %s" % row["doc_url_main"])
            lines.append(u"")
    return lines


def _preskocene(skipped: List[Tuple[dict, str]]) -> List[str]:
    lines = [u"", u"## Nezpracované dokumenty", u""]
    if not skipped:
        lines.append(u"Žádné - do dossieru se vešly všechny nalezené dokumenty.")
        return lines
    lines.append(u"Tyhle dokumenty shortlist u věci našel, ale do dossieru se "
                 u"nedostaly. Otevři si je v ISIR ručně:")
    lines.append(u"")
    for doc, duvod in skipped:
        lines.append(u"- %s (%s, %s) - %s - **%s**" % (
            doc.get("popis") or u"?",
            doc.get("datum") or u"?",
            _KATEGORIE_CZ.get(doc.get("kategorie"), doc.get("kategorie") or u"?"),
            doc.get("url") or u"?",
            duvod,
        ))
    return lines


def build_dossier(spisova_znacka: str, max_docs: int = MAX_DOCS,
                  max_chars: int = MAX_CHARS, write: bool = True) -> Tuple[str, dict]:
    """Slozi dossier k jedne veci. Vraci (markdown, stats).

    Detail veci se stahuje ZNOVU, aby byl seznam dokumentu aktualni (v databazi
    muze byt tydny stary). PDF se berou pres pipeline._load_pdf, takze uz jednou
    stazene dokumenty jdou z cache v data/pdf/ a znovu se netahaji. Prodlevu mezi
    requesty resi http._throttle podle config.REQUEST_DELAY.

    Vyhazuje ValueError, kdyz vec v databazi neni nebo nema detail_id.
    """
    max_docs = max_docs if (max_docs and max_docs > 0) else MAX_DOCS
    max_chars = max_chars if (max_chars and max_chars > 0) else MAX_CHARS

    conn = db.connect()
    db.init_schema(conn)
    try:
        case = _find_case(conn, spisova_znacka)
        prilezitosti = _case_opportunities(conn, case["spisova_znacka"])
    finally:
        conn.close()

    znacka = case["spisova_znacka"]
    if not case.get("detail_id"):
        raise ValueError(u"Věc %s nemá v databázi detail_id - bez něj se detail "
                         u"věci v ISIR nedá otevřít." % znacka)

    log.info("Dossier %s: stahuji detail veci", znacka)
    events = detail.fetch_events(case["detail_id"])
    # Nula udalosti = stranka se nerozparsovala (ISIR vratil chybovou stranku
    # s HTTP 200, nebo se zmenilo markup). Kazda vec v rejstriku ma udalosti
    # desitky az stovky. Kdybychom pokracovali, vznikl by dvoukilobajtovy
    # dossier, ktery tvrdi "do stropu se veslo vsechno, co shortlist nasel"
    # (tedy "u teto veci nic neni"), pretlacil by predchozi dobry soubor na
    # stejnem slugu a v databazi vynuloval shortlist dokumentu. Radeji spadnout.
    if not events:
        raise ValueError(
            u"Detail věci %s nevrátil žádnou událost - ISIR nejspíš odpověděl "
            u"chybovou stránkou. Dossier nesestavuji, aby nepřepsal ten "
            u"předchozí. Zkus to za chvíli znovu." % znacka
        )
    dokumenty = detail.relevant_documents(events)
    soupis = detail.latest_soupis(events)
    _store_case_docs(znacka, soupis, dokumenty)
    log.info("Dossier %s: %d relevantnich dokumentu (%d udalosti celkem)",
             znacka, len(dokumenty), len(events))

    sections = []          # type: List[List[str]]
    skipped = []           # type: List[Tuple[dict, str]]
    used_chars = 0
    zkraceno = False
    chyby = 0

    for doc in dokumenty:
        if len(sections) >= max_docs:
            skipped.append((doc, u"strop %d dokumentů" % max_docs))
            continue
        if used_chars >= max_chars:
            skipped.append((doc, u"strop %s znaků textu" % _cislo(max_chars)))
            continue

        doc_id = _doc_id_from_url(doc.get("url"))
        if not doc_id:
            skipped.append((doc, u"nečitelné ID dokumentu"))
            chyby += 1
            continue

        log.info("Dossier %s: dokument %s (%s) %s", znacka, doc_id,
                 doc.get("kategorie"), (doc.get("popis") or "")[:60])
        try:
            text, source = extract.extract_text(pipeline._load_pdf(doc_id))
        except Exception as exc:
            log.error("Dokument %s selhal: %s", doc_id, exc)
            skipped.append((doc, u"stažení/extrakce selhala: %s" % exc))
            chyby += 1
            continue

        if not (text or "").strip():
            skipped.append((doc, u"z PDF se nepodařilo dostat text (ani OCR)"))
            chyby += 1
            continue

        # Do rozpoctu se pocita jen skutecny text dokumentu, ne poznamka
        # o zkraceni - jinak by hlavicka hlasila vic znaku, nez je strop.
        zbyva = max_chars - used_chars
        poznamka = u""
        if len(text) > zbyva:
            text = text[:zbyva]
            poznamka = (
                u"\n\n[... ZKRÁCENO: dosažen strop %s znaků na celý dossier. "
                u"Zbytek dokumentu si otevři přímo v ISIR: %s ...]"
                % (_cislo(max_chars), doc.get("url") or u"?"))
            zkraceno = True
        used_chars += len(text)
        delka = len(text)
        text = text + poznamka

        fence = _fence(text)
        sections.append([
            u"",
            u"## %s (%s)" % (doc.get("popis") or u"Dokument", doc.get("datum") or u"?"),
            u"",
            u"Kategorie: %s | kód události: %s | zdroj textu: %s | %s znaků | %s"
            % (_KATEGORIE_CZ.get(doc.get("kategorie"), doc.get("kategorie") or u"?"),
               doc.get("kod") or u"?", source, _cislo(delka), doc.get("url") or u"?"),
            u"",
            fence + u"text",
            text.rstrip(),
            fence,
        ])

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    lines = _hlavicka(case, dokumenty, now)
    lines += _limity(len(sections), len(dokumenty), used_chars, max_docs, max_chars, zkraceno)
    lines += _prilezitosti(prilezitosti)
    lines += [u"", u"## Dokumenty (syrový text z ISIR)"]
    if not sections:
        lines += [u"", u"Žádný dokument se nepodařilo zpracovat - viz seznam níže."]
    for section in sections:
        lines += section
    lines += _preskocene(skipped)
    lines.append(u"")
    markdown = u"\n".join(lines)

    slug = slugify(znacka)
    path = config.DOCS_DIR / DOSSIER_SUBDIR / ("%s.md" % slug)
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        log.info("Dossier zapsan: %s (%d B)", path, len(markdown.encode("utf-8")))

    stats = {
        "spisova_znacka": znacka,
        "slug": slug,
        "path": str(path) if write else None,
        "udalosti": len(events),
        "dokumenty_nalezeno": len(dokumenty),
        "dokumenty_zpracovano": len(sections),
        "dokumenty_preskoceno": len(skipped),
        "chyby": chyby,
        "znaku": used_chars,
        "zkraceno": zkraceno,
        "bytes": len(markdown.encode("utf-8")),
    }
    return markdown, stats
