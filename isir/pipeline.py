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


def process_case(conn, record: dict, date_from: date, date_to: date, stats: dict) -> None:
    now = _now_iso()
    db.upsert_case(conn, record, now)

    try:
        events = detail.fetch_events(record["detail_id"])
    except Exception as exc:  # jedna rozbita vec nesmi shodit cely beh
        log.error("Detail %s (%s) selhal: %s", record["detail_id"],
                  record.get("spisova_znacka"), exc)
        stats["errors"] += 1
        return

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


def export_json(path=None) -> dict:
    """Vygeneruje docs/data.json pro dashboard."""
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
                "doc_url": r.get("doc_url_main"),
                "detail_url": r.get("detail_url"),
                "or_url": r.get("or_url"),
                "text_source": r.get("extracted_text_source"),
                "classified_by": r.get("classified_by"),
                "first_seen": created or None,
            }
        )

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
