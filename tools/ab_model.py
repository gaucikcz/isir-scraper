#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A/B porovnani modelu na skutecnych dokumentech z cache.

Nesahá na databazi - jen klasifikuje stejny vstup vice modely a vypise rozdily.
Vzorek je zamerne vychyleny k tezkym pripadum (OCR skeny, cross-reference),
protoze prave tam se levnejsi model rozpada, ne na bezne strojove citelnem PDF.

    python tools/ab_model.py --models claude-sonnet-5,claude-haiku-4-5 --limit 8
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isir import classify, db, extract, pipeline  # noqa: E402

REPORT = "docs/ab_model_report.md"


def _load_text(row):
    """Sestavi presne ten text, ktery by dostala produkcni klasifikace."""
    pdf = pipeline._load_pdf(row["doc_id"])
    text, source = extract.extract_text(pdf)
    if row["crossref_doc_id"]:
        try:
            extra, _ = extract.extract_text(pipeline._load_pdf(row["crossref_doc_id"]))
            if extra.strip():
                text = text + "\n\n=== SOUPIS MAJETKOVE PODSTATY ===\n" + extra
        except Exception as exc:
            print("  cross-ref selhal: %s" % exc)
    return text, source


def _sample(conn, limit):
    """Tezke pripady napred: OCR skeny, pak cross-reference, pak bezne."""
    rows = conn.execute(
        """SELECT e.doc_id, e.extracted_text_source AS src, e.crossref_doc_id,
                  e.event_date, e.spisova_znacka, c.soud, c.dluznik_jmeno, c.ico
           FROM events e LEFT JOIN cases c ON c.spisova_znacka = e.spisova_znacka
           WHERE e.extracted_text_source IS NOT NULL
             AND e.extracted_text_source != 'none'
           ORDER BY CASE e.extracted_text_source WHEN 'ocr' THEN 0 ELSE 1 END,
                    CASE WHEN e.crossref_doc_id IS NOT NULL THEN 0 ELSE 1 END,
                    e.event_date DESC"""
    ).fetchall()
    return [dict(r) for r in rows[:limit]]


def _summarise(result):
    assets = result.get("assets") or []
    return {
        "priorita": result.get("priorita"),
        "poc_polozek": len(assets),
        "typy": [a.get("typ") for a in assets],
        "ceny": [a.get("navrhovana_cena") for a in assets],
        "kupujici": [a.get("kupujici") for a in assets],
        "email": (result.get("spravce") or {}).get("email"),
        "telefon": (result.get("spravce") or {}).get("telefon"),
        "shrnuti": (result.get("shrnuti") or "")[:200],
        "by": result.get("classified_by"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="claude-sonnet-5,claude-haiku-4-5")
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    if not classify._have_credentials():
        print("CHYBA: nejsou k dispozici pristupove udaje k Anthropic API.")
        return 2

    conn = db.connect()
    db.init_schema(conn)
    rows = _sample(conn, args.limit)
    conn.close()
    print("vzorek: %d dokumentu, modely: %s" % (len(rows), ", ".join(models)))

    out = []
    for i, row in enumerate(rows, 1):
        print("[%d/%d] %s (%s)" % (i, len(rows), row["dluznik_jmeno"], row["src"]))
        text, _ = _load_text(row)
        if not text.strip():
            print("  bez textu - preskakuji")
            continue
        meta = {k: row.get(k) for k in ("spisova_znacka", "soud", "dluznik_jmeno", "ico")}
        meta["datum_podani"] = row.get("event_date")

        per_model = {}
        for m in models:
            os.environ["ISIR_MODEL"] = m
            t0 = time.time()
            res = classify.classify_document(text, meta)
            per_model[m] = _summarise(res)
            per_model[m]["sekundy"] = round(time.time() - t0, 1)
            print("   %-22s %s | %d polozek | %s" % (
                m, per_model[m]["by"], per_model[m]["poc_polozek"], per_model[m]["priorita"]))
        out.append({"doc": row["doc_id"], "dluznik": row["dluznik_jmeno"],
                    "src": row["src"], "crossref": bool(row["crossref_doc_id"]),
                    "vysledky": per_model})

    # markdown report
    lines = ["# A/B porovnani modelu", "",
             "Vzorek je zamerne vychyleny k tezkym pripadum (OCR skeny a dokumenty,",
             "ktere popisuji majetek jen odkazem na soupis majetkove podstaty).", ""]
    disagree = 0
    for r in out:
        vals = list(r["vysledky"].values())
        prio = set(v["priorita"] for v in vals)
        cnt = set(v["poc_polozek"] for v in vals)
        flag = ""
        if len(prio) > 1:
            flag = " **<- ROZDILNA PRIORITA**"
            disagree += 1
        elif len(cnt) > 1:
            flag = " <- jiny pocet polozek"
        lines.append("## %s (%s%s)%s" % (r["dluznik"], r["src"],
                                         ", cross-ref" if r["crossref"] else "", flag))
        lines.append("")
        lines.append("| model | priorita | polozek | typy | ceny | e-mail | s |")
        lines.append("|---|---|---|---|---|---|---|")
        for m, v in r["vysledky"].items():
            lines.append("| %s | %s | %d | %s | %s | %s | %s |" % (
                m, v["priorita"], v["poc_polozek"], ", ".join(t or "?" for t in v["typy"]) or "-",
                ", ".join(str(c) for c in v["ceny"] if c) or "-", v["email"] or "-", v["sekundy"]))
        lines.append("")
        for m, v in r["vysledky"].items():
            lines.append("- **%s**: %s" % (m, v["shrnuti"]))
        lines.append("")
    lines.insert(4, "**Rozdilna priorita u %d z %d dokumentu.**\n" % (disagree, len(out)))

    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("\nzapsano %s | rozdilna priorita u %d z %d" % (REPORT, disagree, len(out)))
    print(json.dumps(out, ensure_ascii=False)[:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
