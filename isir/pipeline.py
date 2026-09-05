"""Orchestrace: hledani -> detail -> PDF -> text -> klasifikace -> DB -> data.json."""
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import classify, config, db, detail, extract, http, search

log = logging.getLogger(__name__)

BACKFILL_STATE = config.DATA_DIR / "backfill_state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _cache_pdf(doc_id: str, data: bytes) -> None:
    try:
        config.PDF_DIR.mkdir(parents=True, exist_ok=True)
        (config.PDF_DIR / ("%s.pdf" % doc_id)).write_bytes(data)
    except OSError as exc:
        log.debug("PDF cache selhal pro %s: %s", doc_id, exc)


def _load_pdf(doc_id: str) -> bytes:
    cached = config.PDF_DIR / ("%s.pdf" % doc_id)
    if cached.exists():
        try:
            return cached.read_bytes()
        except OSError:
            pass
    data = http.get_pdf(doc_id)
    _cache_pdf(doc_id, data)
    return data


def _with_case_docs(record: dict, events: list) -> dict:
    """Doplni k zaznamu veci nejnovejsi soupis (kap. 7) a shortlist dokumentu.

    Vraci kopii - puvodni record z vyhledavani nechavame nedotceny.
    Kdyz vec zadny soupis nema, klice zustanou None a upsert_case je diky
    COALESCE nepretlaci pres uz ulozenou hodnotu.

    Shortlist se pocita ze STEJNE stranky detailu, ktera uz je stazena kvuli
    soupisu - nestoji tedy ani jeden request navic. Prazdny seznam se uklada
    take (je to informace "divali jsme se a nic tam neni"), na rozdil od None,
    ktery znamena "nevime".
    """
    row = dict(record)
    soupis = detail.latest_soupis(events)
    if soupis:
        row["soupis_doc_id"] = soupis["doc_id"]
        row["soupis_date"] = soupis["datum"].isoformat() if soupis["datum"] else None
        row["soupis_label"] = soupis.get("popis")
    row["dokumenty_json"] = json.dumps(detail.relevant_documents(events), ensure_ascii=False)
    return row


def process_case(conn, record: dict, date_from: date, date_to: date, stats: dict) -> None:
    now = _now_iso()

    try:
        events = detail.fetch_events(record["detail_id"])
    except Exception as exc:  # jedna rozbita vec nesmi shodit cely beh
        log.error("Detail %s (%s) selhal: %s", record["detail_id"],
                  record.get("spisova_znacka"), exc)
        # Vec presto zaznamename - jen bez soupisu, o kterem nic nevime.
        db.upsert_case(conn, record, now)
        stats["errors"] += 1
        return

    # Soupis i shortlist dokumentu musi byt v zaznamu DRIV, nez se vec ulozi -
    # jinak by se nikdy nezapsaly u veci, ktera v okne zadnou I_347 udalost nema
    # (nize se pak nezpracovava zadny dokument a funkce skonci).
    db.upsert_case(conn, _with_case_docs(record, events), now)

    targets = [
        e for e in detail.find_events(events, config.EVENT_CODE)
        if e["datum"] is not None and date_from <= e["datum"] <= date_to
    ]
    if not targets:
        log.debug("%s: zadna %s udalost v okne", record.get("spisova_znacka"), config.EVENT_CODE)

    for ev in targets:
        if not ev["doc_id"]:
            log.info("%s %s: udalost bez dokumentu - preskakuji",
                     record.get("spisova_znacka"), ev["datum"])
            stats["no_document"] += 1
            continue
        if db.have_doc(conn, ev["doc_id"]):
            stats["skipped_known"] += 1
            continue

        try:
            _process_document(conn, record, events, ev, now, stats)
        except Exception as exc:
            log.error("Dokument %s selhal: %s", ev["doc_id"], exc)
            stats["errors"] += 1

    conn.commit()


def _process_document(conn, record, events, ev, now, stats) -> None:
    pdf_bytes = _load_pdf(ev["doc_id"])
    text, source = extract.extract_text(pdf_bytes)
    stats["downloaded"] += 1

    crossref_doc_id = None
    if not text.strip():
        log.warning("Dokument %s: nepodarilo se ziskat text", ev["doc_id"])
    elif classify.needs_soupis_crossref(text):
        # Navrh popisuje majetek jen odkazem na soupis - dohledat ho (kap. 7).
        soupis = detail.nearest_preceding(events, config.SOUPIS_CODES, ev["datum"])
        if soupis:
            try:
                extra_text, _ = extract.extract_text(_load_pdf(soupis["doc_id"]))
                if extra_text.strip():
                    text = text + "\n\n=== SOUPIS MAJETKOVE PODSTATY ===\n" + extra_text
                    crossref_doc_id = soupis["doc_id"]
                    stats["crossref"] += 1
                    log.info("Dokument %s doplnen o soupis %s", ev["doc_id"], soupis["doc_id"])
            except Exception as exc:
                log.warning("Cross-ref soupis %s selhal: %s", soupis["doc_id"], exc)

    case_meta = {
        "spisova_znacka": record.get("spisova_znacka"),
        "soud": record.get("soud"),
        "dluznik_jmeno": record.get("dluznik_jmeno"),
        "ico": record.get("ico"),
        "datum_podani": ev["datum"].isoformat() if ev["datum"] else None,
    }
    result = classify.classify_document(text, case_meta)

    ev_row = dict(ev)
    ev_row["spisova_znacka"] = record.get("spisova_znacka")
    ev_row["event_date"] = ev["datum"].isoformat() if ev["datum"] else None
    ev_row["text_source"] = source
    ev_row["crossref_doc_id"] = crossref_doc_id
    db.insert_event(conn, ev_row, now)
    db.insert_opportunity(conn, ev["doc_id"], result, now)

    stats["new"] += 1
    stats[result.get("priorita", "nizka")] = stats.get(result.get("priorita", "nizka"), 0) + 1
    log.info("NOVE: %s | %s | %s | %s",
             record.get("dluznik_jmeno"), ev["datum"],
             result.get("priorita"), (result.get("shrnuti") or "")[:80])


def _new_stats() -> dict:
    return {"cases": 0, "new": 0, "skipped_known": 0, "no_document": 0,
            "downloaded": 0, "crossref": 0, "errors": 0,
            "vysoka": 0, "stredni": 0, "nizka": 0}


def run_window(date_from: date, date_to: date, conn=None) -> dict:
    own = conn is None
    if own:
        conn = db.connect()
        db.init_schema(conn)
    stats = _new_stats()
    try:
        records = search.search_window(date_from, date_to)
        stats["cases"] = len(records)
        log.info("Okno %s..%s: %d dluzniku ke zpracovani", date_from, date_to, len(records))
        for i, rec in enumerate(records, 1):
            log.info("[%d/%d] %s - %s", i, len(records),
                     rec.get("spisova_znacka"), rec.get("dluznik_jmeno"))
            process_case(conn, rec, date_from, date_to, stats)
    finally:
        conn.commit()
        if own:
            conn.close()
    return stats


def run_daily(days: int = 10) -> dict:
    """Posuvne okno dnes-N .. dnes. N=10 je rezerva proti zpozdenemu zverejneni."""
    today = date.today()
    start = today - timedelta(days=max(0, days))
    log.info("=== DENNI BEH: %s .. %s ===", start, today)
    stats = run_window(start, today)
    log.info("=== HOTOVO: %s ===", stats)
    return stats


def run_backfill(date_from: date, date_to: Optional[date] = None, resume: bool = True) -> dict:
    """Historicky sber po 30dennich oknech, s moznosti pauza/resume (kap. 11)."""
    date_to = date_to or date.today()
    if resume and BACKFILL_STATE.exists():
        try:
            saved = json.loads(BACKFILL_STATE.read_text(encoding="utf-8"))
            resume_from = date.fromisoformat(saved["next_from"])
            if date_from <= resume_from <= date_to:
                log.info("Navazuji na predchozi backfill od %s", resume_from)
                date_from = resume_from
        except (ValueError, KeyError, OSError) as exc:
            log.warning("Nelze precist backfill state: %s", exc)

    conn = db.connect()
    db.init_schema(conn)
    total = _new_stats()
    cursor = date_from
    try:
        while cursor <= date_to:
            chunk_end = min(cursor + timedelta(days=config.MAX_WINDOW_DAYS - 1), date_to)
            log.info("--- backfill okno %s .. %s ---", cursor, chunk_end)
            try:
                stats = run_window(cursor, chunk_end, conn=conn)
                for k, v in stats.items():
                    total[k] = total.get(k, 0) + v
            except Exception as exc:
                log.error("Okno %s..%s selhalo: %s", cursor, chunk_end, exc)
                total["errors"] += 1
            cursor = chunk_end + timedelta(days=1)
            try:
                config.DATA_DIR.mkdir(parents=True, exist_ok=True)
                BACKFILL_STATE.write_text(
                    json.dumps({"next_from": cursor.isoformat(),
                                "target_to": date_to.isoformat(),
                                "updated_at": _now_iso()}, indent=2),
                    encoding="utf-8",
                )
            except OSError as exc:
                log.warning("Nelze ulozit backfill state: %s", exc)
    finally:
        conn.commit()
        conn.close()
    log.info("=== BACKFILL HOTOVO: %s ===", total)
    return total


# ---------------------------------------------------------------------------
# Doplneni odkazu na soupis majetkove podstaty k uz ulozenym vecem
# ---------------------------------------------------------------------------

# Fronta se ridi tim, kdy jsme u veci naposledy koukali po soupisu, ne tim,
# jestli uz nejaky ma. Vetsina veci zadny soupis nikdy mit nebude - kdyby se
# radilo jen podle last_checked_at, tyhle "nenalezeno" veci by navzdy sedely v
# cele fronty a beh s --limit by k dalsim vecem nikdy nedosel (tise, bez chyby).
# NULL (nikdy nekontrolovano) se v SQLite pri ASC radi prvni = nove veci maji
# prednost, pak nejdele nekontrolovane. Beh tak vzdycky postoupi a postupne
# fronta rotuje, takze se zachyti i pozdejsi "- doplneni" / "- zmena".
CASE_DOCS_SQL = """
SELECT spisova_znacka, detail_id, dluznik_jmeno, soupis_doc_id, soupis_date, dokumenty_json
FROM cases
WHERE detail_id IS NOT NULL AND detail_id <> ''
%s
ORDER BY soupis_checked_at ASC, last_checked_at DESC, spisova_znacka
%s
"""


def _soupis_stamp() -> str:
    """Razitko kontroly soupisu - na rozdil od _now_iso() VCETNE mikrosekund.

    Razitko je tridici klic fronty. Kdyby dve veci zkontrolovane v jednom behu
    dostaly stejnou hodnotu (a _now_iso() zaokrouhluje na sekundy), rozhodl by
    az stabilni druhotny klic a fronta by se zase zasekla na stejne hlave.
    """
    return datetime.now(timezone.utc).isoformat()


def _mark_soupis_checked(conn, znacka: str, now: str) -> None:
    """Zapise, ze vec uz byla na soupis kontrolovana (posun ve fronte)."""
    conn.execute("UPDATE cases SET soupis_checked_at=? WHERE spisova_znacka=?",
                 (now, znacka))
    conn.commit()  # prubezne - timeout nesmi zahodit hotovou praci


def refresh_case_docs(limit: Optional[int] = None, only_missing: bool = True) -> dict:
    """Doplni k ulozenym vecem soupis majetkove podstaty A shortlist dokumentu.

    Veci nasbirane driv ani jedno v databazi nemaji - tenhle beh to dohleda.
    Obe veci se ctou z JEDNE stazene stranky detailu (oddil B), takze shortlist
    dokumentu nestoji ani jeden request navic oproti puvodnimu behu po soupisech.

    Beh je POUZE HTTP: stahuje jen HTML detailu veci (jeden request na vec).
    ZADNE PDF se nestahuje a NEVOLA se LLM - workflow proto nepotrebuje
    ANTHROPIC_API_KEY a beh nestoji nic krome par requestu.

    only_missing=False vezme i veci, ktere uz odkaz maji: soupis i seznam
    dokumentu se v case meni ("- doplneni", "- zmena", nove usneseni) a
    novejsi verze prepise starsi.
    """
    stats = {"cases": 0, "updated": 0, "dokumenty": 0, "nenalezeno": 0, "errors": 0}

    conn = db.connect()
    db.init_schema(conn)
    try:
        where = ""
        if only_missing:
            # "Chybi" = chybi soupis NEBO chybi shortlist dokumentu. Vec, ktera
            # uz soupis ma, ale je z doby pred shortlistem, se tim dostane do
            # fronty taky - jinak by k dokumentum nikdy neprisla.
            where = ("AND ((soupis_doc_id IS NULL OR soupis_doc_id = '')\n"
                     "     OR dokumenty_json IS NULL OR dokumenty_json = '')")
        tail = ""
        params = ()
        if limit is not None and limit > 0:
            tail = "LIMIT ?"
            params = (int(limit),)
        rows = [dict(r) for r in conn.execute(CASE_DOCS_SQL % (where, tail), params).fetchall()]
        stats["cases"] = len(rows)
        log.info("=== SOUPISY + DOKUMENTY: %d veci ke kontrole (only_missing=%s) ===",
                 len(rows), only_missing)

        for i, row in enumerate(rows, 1):
            znacka = row.get("spisova_znacka")
            log.info("[%d/%d] %s - %s", i, len(rows), znacka, row.get("dluznik_jmeno"))
            # Razitko se zapisuje u KAZDE zkontrolovane veci - i kdyz zadny
            # soupis nema, i kdyz se detail nestahl. Jinak by se stejna vec
            # tahala v kazdem behu znovu a fronta by se nikam neposunula.
            checked = _soupis_stamp()
            try:
                events = detail.fetch_events(row["detail_id"])
            except Exception as exc:  # jedna rozbita vec nesmi shodit cely beh
                log.error("Detail %s (%s) selhal: %s", row["detail_id"], znacka, exc)
                stats["errors"] += 1
                _mark_soupis_checked(conn, znacka, checked)
                continue

            try:
                soupis = detail.latest_soupis(events)
                dokumenty = detail.relevant_documents(events)
            except Exception as exc:
                log.error("Vyhodnoceni detailu u %s selhalo: %s", znacka, exc)
                stats["errors"] += 1
                _mark_soupis_checked(conn, znacka, checked)
                continue

            # Razitko kontroly se meni vzdycky, obsah jen kdyz je opravdu novy.
            sets = ["soupis_checked_at=?"]
            params = [checked]

            if not soupis:
                stats["nenalezeno"] += 1
                log.info("BEZ SOUPISU: %s", znacka)
            else:
                datum = soupis["datum"].isoformat() if soupis["datum"] else None
                if soupis["doc_id"] == row.get("soupis_doc_id") \
                        and datum == row.get("soupis_date"):
                    log.debug("%s: soupis %s uz je aktualni", znacka, soupis["doc_id"])
                else:
                    sets += ["soupis_doc_id=?", "soupis_date=?", "soupis_label=?"]
                    params += [soupis["doc_id"], datum, soupis.get("popis")]
                    stats["updated"] += 1
                    log.info("SOUPIS: %s | %s | %s | %s", znacka, datum,
                             soupis["doc_id"], (soupis.get("popis") or "")[:60])

            # Prazdny seznam ("[]") se uklada taky - je to informace "divali
            # jsme se a nic relevantniho tam neni", ne "nevime".
            dokumenty_json = json.dumps(dokumenty, ensure_ascii=False)
            if dokumenty_json != (row.get("dokumenty_json") or ""):
                sets.append("dokumenty_json=?")
                params.append(dokumenty_json)
                stats["dokumenty"] += 1
                log.info("DOKUMENTY: %s | %d ks | %s", znacka, len(dokumenty),
                         ", ".join(sorted(set(d["kategorie"] for d in dokumenty))) or "-")

            params.append(znacka)
            conn.execute("UPDATE cases SET %s WHERE spisova_znacka=?" % ", ".join(sets),
                         params)
            conn.commit()  # prubezne - timeout nesmi zahodit hotovou praci
    finally:
        conn.commit()
        conn.close()

    log.info("=== SOUPISY + DOKUMENTY HOTOVO: %s ===", stats)
    return stats


# Puvodni jmeno funkce. Beh uz nedohledava jen soupis, ale i shortlist
# dokumentu; alias drzime, aby nic zvenci (workflow, skripty) neprestalo fungovat.
refresh_soupis = refresh_case_docs


# ---------------------------------------------------------------------------
# Preklasifikace ulozenych prilezitosti (heuristic -> llm)
# ---------------------------------------------------------------------------

RECLASSIFY_SQL = """
SELECT o.id, o.doc_id, o.priorita, o.assets_json, o.shrnuti, o.classified_by, o.status,
       e.event_date, e.crossref_doc_id, e.spisova_znacka,
       c.soud, c.dluznik_jmeno, c.ico
FROM opportunities o
JOIN events e ON e.doc_id = o.doc_id
LEFT JOIN cases c ON c.spisova_znacka = e.spisova_znacka
%s
ORDER BY e.event_date DESC, o.id DESC
%s
"""


def _reclassify_candidates(conn, only_heuristic: bool = True, force: bool = False,
                           limit: Optional[int] = None) -> list:
    """Radky ke zpracovani, nejnovejsi udalosti prvni (cerstve podani je cennejsi).

    cases pripojujeme pres LEFT JOIN stejne jako db.all_opportunities - kdyz by
    radek v cases chybel, prilezitost nesmi z vyberu vypadnout.
    """
    where = ""
    if not force and only_heuristic:
        where = "WHERE o.classified_by IS NULL OR o.classified_by = 'heuristic'"
    tail = ""
    params = ()
    if limit is not None and limit > 0:
        tail = "LIMIT ?"
        params = (int(limit),)
    rows = conn.execute(RECLASSIFY_SQL % (where, tail), params).fetchall()
    return [dict(r) for r in rows]


def _reclassify_text(row: dict) -> str:
    """Slozi vstup pro klasifikator presne jako _process_document (vcetne soupisu)."""
    text, _source = extract.extract_text(_load_pdf(row["doc_id"]))
    if not text.strip():
        return ""

    crossref = row.get("crossref_doc_id")
    if crossref:
        try:
            extra_text, _ = extract.extract_text(_load_pdf(crossref))
            if extra_text.strip():
                text = text + "\n\n=== SOUPIS MAJETKOVE PODSTATY ===\n" + extra_text
        except Exception as exc:
            log.warning("Soupis %s k dokumentu %s se nepodarilo nacist: %s",
                        crossref, row["doc_id"], exc)
    elif classify.needs_soupis_crossref(text):
        # Detailni stranku uz zpetne neotevirame - jen zaznamename, ze by
        # dokumentu soupis prospel (doplni se pri pripadnem novem sberu).
        log.info("Dokument %s odkazuje na soupis, ale crossref chybi - "
                 "klasifikuji bez nej", row["doc_id"])
    return text


def _same_result(row: dict, result: dict) -> bool:
    """Nova klasifikace se od ulozeneho radku nicim nelisi (vcetne puvodu)."""
    if (row.get("classified_by") or "") != result.get("classified_by"):
        return False
    if (row.get("priorita") or "") != (result.get("priorita") or "nizka"):
        return False
    if (row.get("shrnuti") or "") != (result.get("shrnuti") or ""):
        return False
    old_assets = row.get("assets_json") or "[]"
    new_assets = json.dumps(result.get("assets") or [], ensure_ascii=False)
    return old_assets == new_assets


def _write_reclassified(conn, row: dict, result: dict, now: str) -> None:
    """Zapis vysledku tak, aby prezil rucne nastaveny 'status'.

    db.insert_opportunity() ve vetvi ON CONFLICT DO UPDATE sloupec 'status'
    (ani 'created_at') nepresepisuje, takze rucni oznaceni z dashboardu zustava.
    Presto si puvodni hodnotu drzime a po zapisu ji zkontrolujeme - kdyby se
    UPSERT v db.py nekdy zmenil, preklasifikace uzivateli jeho stav nesmaze.
    """
    previous_status = row.get("status")
    db.insert_opportunity(conn, row["doc_id"], result, now)
    current = conn.execute(
        "SELECT status FROM opportunities WHERE doc_id=?", (row["doc_id"],)
    ).fetchone()
    if previous_status is not None and current is not None \
            and current["status"] != previous_status:
        conn.execute("UPDATE opportunities SET status=? WHERE doc_id=?",
                     (previous_status, row["doc_id"]))
        log.warning("Status dokumentu %s byl pri zapisu prepsan - obnoven na %r",
                    row["doc_id"], previous_status)


def run_reclassify(limit: Optional[int] = None, only_heuristic: bool = True,
                   force: bool = False, dry_run: bool = False) -> dict:
    """Preklasifikuje jiz ulozene prilezitosti pomoci LLM.

    Denni beh dedupuje na doc_id, takze radky posbirane bez funkcniho API klice
    by uz nikdy nebyly klasifikovany lepe. Tenhle beh je vezme znovu.

    Zapisujeme JEN kdyz klasifikator vratil classified_by == "llm" - vypadek API
    by jinak tise prepsal dobre radky horsi heuristikou.

    dry_run nesaha na sit, na API ani do databaze: pouze spocita kandidaty.
    """
    stats = {"candidates": 0, "reclassified": 0, "unchanged": 0,
             "no_text": 0, "failed": 0, "aborted": False}

    conn = db.connect()
    db.init_schema(conn)
    try:
        rows = _reclassify_candidates(conn, only_heuristic=only_heuristic,
                                      force=force, limit=limit)
        stats["candidates"] = len(rows)

        if dry_run:
            cached = sum(1 for r in rows
                         if (config.PDF_DIR / ("%s.pdf" % r["doc_id"])).exists())
            log.info("DRY RUN: ke zpracovani %d prilezitosti "
                     "(%d PDF v cache, %d by se stahovalo), zadne volani API",
                     len(rows), cached, len(rows) - cached)
            for r in rows[:10]:
                log.info("  by se preklasifikovalo: %s | %s | %s | %s",
                         r.get("dluznik_jmeno"), r.get("event_date"),
                         r.get("classified_by"), r.get("priorita"))
            if len(rows) > 10:
                log.info("  ... a dalsich %d", len(rows) - 10)
            if not classify._have_credentials():
                log.warning("Pozor: bez ANTHROPIC_API_KEY by ostry beh skoncil "
                            "hned na zacatku (nebylo by cim klasifikovat).")
            return stats

        if not classify._have_credentials():
            log.error("PRERUSENO: neni k dispozici ANTHROPIC_API_KEY (ani workload "
                      "identity federation). Preklasifikace bez LLM by jen prepsala "
                      "heuristiku heuristikou - beh nema smysl. Nastavte klic a "
                      "spustte znovu.")
            stats["aborted"] = True
            return stats

        log.info("=== PREKLASIFIKACE: %d kandidatu (force=%s) ===", len(rows), force)
        now = _now_iso()
        for i, row in enumerate(rows, 1):
            doc_id = row["doc_id"]
            log.info("[%d/%d] %s | %s | %s", i, len(rows), doc_id,
                     row.get("dluznik_jmeno"), row.get("event_date"))
            try:
                text = _reclassify_text(row)
            except Exception as exc:
                log.error("Dokument %s: nacteni/extrakce selhala: %s", doc_id, exc)
                stats["failed"] += 1
                continue

            if not text.strip():
                log.warning("Dokument %s: prazdny text - neni co klasifikovat", doc_id)
                stats["no_text"] += 1
                continue

            case_meta = {
                "spisova_znacka": row.get("spisova_znacka"),
                "soud": row.get("soud"),
                "dluznik_jmeno": row.get("dluznik_jmeno"),
                "ico": row.get("ico"),
                "datum_podani": row.get("event_date"),
            }
            try:
                result = classify.classify_document(text, case_meta)
            except Exception as exc:
                log.error("Dokument %s: klasifikace selhala: %s", doc_id, exc)
                stats["failed"] += 1
                continue

            if result.get("classified_by") != "llm":
                # Rate limit / chyba API -> classify_document degradoval na
                # heuristiku. Puvodni radek je minimalne stejne dobry, nechavame ho.
                log.warning("Dokument %s: LLM nedostupny (vratil %r) - ponechavam "
                            "puvodni zaznam", doc_id, result.get("classified_by"))
                stats["failed"] += 1
                continue

            if _same_result(row, result):
                stats["unchanged"] += 1
                log.info("BEZE ZMENY: %s | %s", row.get("dluznik_jmeno"), doc_id)
                continue

            _write_reclassified(conn, row, result, now)
            conn.commit()  # po kazdem radku - timeout nesmi zahodit hotovou praci
            stats["reclassified"] += 1
            log.info("PREKLASIFIKOVANO: %s | %s | %s -> llm | %s",
                     row.get("dluznik_jmeno"), row.get("event_date"),
                     row.get("classified_by") or "neznamy", result.get("priorita"))
    finally:
        conn.commit()
        conn.close()

    log.info("=== PREKLASIFIKACE HOTOVO: %s ===", stats)
    return stats


def export_json(path=None) -> dict:
    """Vygeneruje docs/data.json pro dashboard."""
    # Import az tady: dossier.py importuje pipeline (kvuli _load_pdf a PDF cache),
    # takze opacny import na urovni modulu by byl kruhovy.
    from . import dossier as dossier_mod

    path = path or config.DATA_JSON
    conn = db.connect()
    db.init_schema(conn)
    rows = db.all_opportunities(conn)
    conn.close()

    today = date.today()
    week_ago = today - timedelta(days=7)
    opportunities = []
    for r in rows:
        try:
            assets = json.loads(r.get("assets_json") or "[]")
        except (ValueError, TypeError):
            assets = []
        created = (r.get("created_at") or "")[:10]
        # Shortlist relevantnich dokumentu veci. Zadny jeden typ dokumentu neni
        # spolehlive pritomny, takze dashboard nabizi rovnou cely kratky seznam.
        try:
            dokumenty = json.loads(r.get("dokumenty_json") or "[]")
        except (ValueError, TypeError):
            dokumenty = []
        if not isinstance(dokumenty, list):
            dokumenty = []
        # Navrh casto popisuje majetek jen odkazem na soupis - dashboard proto
        # nabizi primy odkaz na nejnovejsi soupis. Vec ho mit nemusi -> null.
        soupis_doc_id = r.get("soupis_doc_id")
        soupis = None
        if soupis_doc_id:
            soupis = {
                "url": "%s?id=%s" % (config.DOC_URL, soupis_doc_id),
                "datum": r.get("soupis_date"),
                "popis": r.get("soupis_label"),
            }
        opportunities.append(
            {
                "doc_id": r["doc_id"],
                "spisova_znacka": r.get("spisova_znacka"),
                "soud": r.get("soud"),
                "dluznik_jmeno": r.get("dluznik_jmeno"),
                "ico": r.get("ico"),
                "sidlo": r.get("sidlo"),
                "stav_rizeni": r.get("stav_rizeni"),
                "datum_podani": r.get("event_date"),
                "priorita": r.get("priorita") or "nizka",
                "assets": assets,
                "spravce": {
                    "jmeno": r.get("spravce_jmeno"),
                    "email": r.get("spravce_email"),
                    "telefon": r.get("spravce_telefon"),
                    "datova_schranka": r.get("spravce_datova_schranka"),
                },
                "shrnuti": r.get("shrnuti"),
                "soupis": soupis,
                "dokumenty": dokumenty,
                "doc_url": r.get("doc_url_main"),
                "detail_url": r.get("detail_url"),
                "or_url": r.get("or_url"),
                "text_source": r.get("extracted_text_source"),
                "classified_by": r.get("classified_by"),
                "first_seen": created or None,
            }
        )
        # Uz sestaveny dossier (docs/dossier/<slug>.md) - dashboard je servirovan
        # z docs/, takze odkaz je relativni. Slug pocita primo modul dossier,
        # aby se logika nerozesla se jmenem souboru, ktery skutecne vznikl.
        znacka = r.get("spisova_znacka")
        if znacka:
            slug = dossier_mod.slugify(znacka)
            if (config.DOCS_DIR / dossier_mod.DOSSIER_SUBDIR / ("%s.md" % slug)).exists():
                opportunities[-1]["dossier_url"] = "%s/%s.md" % (dossier_mod.DOSSIER_SUBDIR, slug)

    def _is_new(o):
        fs = o.get("first_seen")
        if not fs:
            return False
        try:
            return date.fromisoformat(fs) >= week_ago
        except ValueError:
            return False

    dates = [o["datum_podani"] for o in opportunities if o.get("datum_podani")]
    payload = {
        "generated_at": _now_iso(),
        "window": {"from": min(dates) if dates else None, "to": max(dates) if dates else None},
        "stats": {
            "total": len(opportunities),
            "vysoka": sum(1 for o in opportunities if o["priorita"] == "vysoka"),
            "stredni": sum(1 for o in opportunities if o["priorita"] == "stredni"),
            "nizka": sum(1 for o in opportunities if o["priorita"] == "nizka"),
            "new_7d": sum(1 for o in opportunities if _is_new(o)),
            "cases": len(set(o.get("spisova_znacka") for o in opportunities)),
        },
        "opportunities": opportunities,
    }

    config.DOCS_DIR.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    log.info("Zapsano %s (%d prilezitosti)", path, len(opportunities))
    return payload["stats"]
