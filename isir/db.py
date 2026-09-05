"""SQLite uloziste. doc_id (ciselne ID PDF) je primarni dedup klic."""
import hashlib
import json
import logging
import sqlite3
from typing import List, Optional

from . import config

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  spisova_znacka   TEXT PRIMARY KEY,
  soud             TEXT,
  dluznik_jmeno    TEXT,
  ico              TEXT,
  rodne_cislo_hash TEXT,
  stav_rizeni      TEXT,
  sidlo            TEXT,
  detail_id        TEXT,
  detail_url       TEXT,
  or_url           TEXT,
  first_seen_at    TEXT,
  last_checked_at  TEXT,
  soupis_doc_id    TEXT,
  soupis_date      TEXT,
  soupis_label     TEXT,
  soupis_checked_at TEXT,
  dokumenty_json   TEXT
);

CREATE TABLE IF NOT EXISTS events (
  doc_id                TEXT PRIMARY KEY,
  spisova_znacka        TEXT REFERENCES cases(spisova_znacka),
  event_type_code       TEXT,
  event_label           TEXT,
  event_date            TEXT,
  event_time            TEXT,
  doc_url_main          TEXT,
  doc_url_side          TEXT,
  extracted_text_source TEXT,
  crossref_doc_id       TEXT,
  processed_at          TEXT
);

CREATE TABLE IF NOT EXISTS opportunities (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id                  TEXT UNIQUE REFERENCES events(doc_id),
  priorita                TEXT,
  assets_json             TEXT,
  spravce_jmeno           TEXT,
  spravce_email           TEXT,
  spravce_telefon         TEXT,
  spravce_datova_schranka TEXT,
  shrnuti                 TEXT,
  classified_by           TEXT,
  status                  TEXT DEFAULT 'new',
  ocenani_json            TEXT,
  nabidka_json            TEXT,
  created_at              TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_case ON events(spisova_znacka);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date);
CREATE INDEX IF NOT EXISTS idx_opp_priorita ON opportunities(priorita);
"""

# Sloupce doplnene po prvnim nasazeni. SCHEMA vyse pouziva CREATE TABLE IF NOT
# EXISTS, takze do UZ EXISTUJICI databaze (data/isir.sqlite3 je v repu) se novy
# sloupec sam nedostane - musi se pridat pres ALTER TABLE. Radek = (tabulka,
# sloupec, deklarace). Migrace je idempotentni, chybejici tabulku preskoci.
MIGRATIONS = (
    ("cases", "soupis_doc_id", "TEXT"),
    ("cases", "soupis_date", "TEXT"),
    ("cases", "soupis_label", "TEXT"),
    # Kdy jsme u veci naposledy koukali po soupisu (at uz jsme nejaky nasli
    # nebo ne). Ridi frontu v pipeline.refresh_soupis - bez toho by beh s
    # --limit porad dokola stahoval tytez veci a k dalsim se nedostal.
    ("cases", "soupis_checked_at", "TEXT"),
    # Shortlist relevantnich dokumentu veci (JSON pole z detail.relevant_documents).
    # Zadny jeden typ dokumentu neni spolehlive pritomny (mereni na 20 vecech:
    # usneseni o prodeji 50 %, soupis 30 %, katastr 25 %, zprava spravce 25 %,
    # znalecky posudek 0 %), takze u veci drzime rovnou cely kratky seznam.
    ("cases", "dokumenty_json", "TEXT"),
)


def hash_rc(rodne_cislo: Optional[str]) -> Optional[str]:
    """Rodne cislo nikdy neukladame v plaintextu (GDPR, kap. 1)."""
    if not rodne_cislo:
        return None
    digits = "".join(ch for ch in rodne_cislo if ch.isdigit())
    if not digits:
        return None
    return hashlib.sha256(digits.encode("utf-8")).hexdigest()


def connect(path=None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_columns(conn, table: str) -> List[str]:
    """Nazvy sloupcu tabulky; prazdny seznam kdyz tabulka neexistuje."""
    return [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]


def migrate(conn) -> List[str]:
    """Dorovna schema uz existujici databaze (ALTER TABLE ADD COLUMN).

    Volat vzdy po SCHEMA. Bezpecne opakovatelne: co uz existuje, se preskoci.
    Vraci seznam skutecne pridanych sloupcu (kvuli logu).
    """
    added = []
    columns_cache = {}
    for table, column, decl in MIGRATIONS:
        if table not in columns_cache:
            columns_cache[table] = _table_columns(conn, table)
        existing = columns_cache[table]
        if not existing:
            log.debug("Migrace: tabulka %s neexistuje, preskakuji", table)
            continue
        if column in existing:
            continue
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))
        existing.append(column)
        added.append("%s.%s" % (table, column))
    if added:
        log.info("Migrace databaze: pridano %s", ", ".join(added))
    return added


def init_schema(conn) -> None:
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()


def upsert_case(conn, case: dict, now: str) -> None:
    conn.execute(
        """
        INSERT INTO cases (spisova_znacka, soud, dluznik_jmeno, ico, rodne_cislo_hash,
                           stav_rizeni, sidlo, detail_id, detail_url, or_url,
                           first_seen_at, last_checked_at,
                           soupis_doc_id, soupis_date, soupis_label, dokumenty_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(spisova_znacka) DO UPDATE SET
          soud=excluded.soud, dluznik_jmeno=excluded.dluznik_jmeno, ico=excluded.ico,
          rodne_cislo_hash=excluded.rodne_cislo_hash, stav_rizeni=excluded.stav_rizeni,
          sidlo=excluded.sidlo, detail_id=excluded.detail_id, detail_url=excluded.detail_url,
          or_url=excluded.or_url, last_checked_at=excluded.last_checked_at,
          -- Kdyz volajici o soupisu nic nevi (napr. selhal detail veci), uz
          -- znamy odkaz se NESMI prepsat na NULL - proto COALESCE.
          soupis_doc_id=COALESCE(excluded.soupis_doc_id, cases.soupis_doc_id),
          soupis_date=COALESCE(excluded.soupis_date, cases.soupis_date),
          soupis_label=COALESCE(excluded.soupis_label, cases.soupis_label),
          -- Totez pro shortlist dokumentu: zapis bez informace o dokumentech
          -- (napr. po selhani detailu veci) nesmi uz ulozeny seznam vynulovat.
          dokumenty_json=COALESCE(excluded.dokumenty_json, cases.dokumenty_json)
        """,
        (
            case["spisova_znacka"], case.get("soud"), case.get("dluznik_jmeno"),
            case.get("ico"), hash_rc(case.get("rodne_cislo_raw")), case.get("stav_rizeni"),
            case.get("sidlo"), case.get("detail_id"), case.get("detail_url"),
            case.get("or_url"), now, now,
            case.get("soupis_doc_id"), case.get("soupis_date"), case.get("soupis_label"),
            case.get("dokumenty_json"),
        ),
    )


def have_doc(conn, doc_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM events WHERE doc_id=?", (doc_id,)).fetchone()
    return row is not None


def insert_event(conn, ev: dict, now: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO events
          (doc_id, spisova_znacka, event_type_code, event_label, event_date, event_time,
           doc_url_main, doc_url_side, extracted_text_source, crossref_doc_id, processed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            ev["doc_id"], ev.get("spisova_znacka"), ev.get("kod"), ev.get("popis"),
            ev["event_date"], ev.get("cas"), ev.get("doc_url"), ev.get("doc_url_side"),
            ev.get("text_source"), ev.get("crossref_doc_id"), now,
        ),
    )


def insert_opportunity(conn, doc_id: str, result: dict, now: str) -> None:
    spravce = result.get("spravce") or {}
    conn.execute(
        """
        INSERT INTO opportunities
          (doc_id, priorita, assets_json, spravce_jmeno, spravce_email, spravce_telefon,
           spravce_datova_schranka, shrnuti, classified_by, status, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,'new',?)
        ON CONFLICT(doc_id) DO UPDATE SET
          priorita=excluded.priorita, assets_json=excluded.assets_json,
          spravce_jmeno=excluded.spravce_jmeno, spravce_email=excluded.spravce_email,
          spravce_telefon=excluded.spravce_telefon,
          spravce_datova_schranka=excluded.spravce_datova_schranka,
          shrnuti=excluded.shrnuti, classified_by=excluded.classified_by
        """,
        (
            doc_id, result.get("priorita", "nizka"),
            json.dumps(result.get("assets") or [], ensure_ascii=False),
            spravce.get("jmeno"), spravce.get("email"), spravce.get("telefon"),
            spravce.get("datova_schranka"), result.get("shrnuti"),
            result.get("classified_by"), now,
        ),
    )


def all_opportunities(conn) -> List[dict]:
    rows = conn.execute(
        """
        SELECT o.*, e.event_date, e.event_label, e.doc_url_main, e.extracted_text_source,
               e.spisova_znacka, c.soud, c.dluznik_jmeno, c.ico, c.sidlo, c.stav_rizeni,
               c.detail_url, c.or_url,
               c.soupis_doc_id, c.soupis_date, c.soupis_label, c.dokumenty_json
        FROM opportunities o
        JOIN events e ON e.doc_id = o.doc_id
        LEFT JOIN cases c ON c.spisova_znacka = e.spisova_znacka
        ORDER BY e.event_date DESC, o.id DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def soupis_coverage(conn) -> dict:
    """Kolik veci uz ma odkaz na soupis majetkove podstaty (pro souhrny)."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS celkem,
               SUM(CASE WHEN soupis_doc_id IS NOT NULL AND soupis_doc_id <> ''
                        THEN 1 ELSE 0 END) AS se_soupisem
        FROM cases
        """
    ).fetchone()
    return {"cases": row["celkem"] or 0, "se_soupisem": row["se_soupisem"] or 0}
