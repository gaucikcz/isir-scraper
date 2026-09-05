#!/usr/bin/env python3
"""ISIR scraper CLI.

  python cli.py daily [--days 10]
  python cli.py backfill --from 2024-01-01 [--to 2024-12-31] [--no-resume]
  python cli.py export
  python cli.py reclassify [--limit 25] [--all] [--dry-run]
  python cli.py refresh-soupis [--limit 200] [--all]
  python cli.py dossier "KSOS 37 INS 16018/2019" [--max-docs 8] [--stdout]
  python cli.py search --from 2026-08-10 --to 2026-09-05   (jen vypis, nic neuklada)
"""
import argparse
import logging
import sys
from datetime import date, timedelta

from isir import config, dossier, pipeline, search


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("ocekavan format RRRR-MM-DD, dostal jsem %r" % value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cli.py", description="ISIR distressed-asset scraper")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    p_daily = sub.add_parser("daily", help="denni inkrementalni beh")
    p_daily.add_argument("--days", type=int, default=10,
                         help="sirka posuvneho okna ve dnech (default 10)")
    p_daily.add_argument("--no-export", action="store_true")

    p_back = sub.add_parser("backfill", help="historicky sber po 30dennich oknech")
    p_back.add_argument("--from", dest="date_from", type=_date, required=True)
    p_back.add_argument("--to", dest="date_to", type=_date, default=None)
    p_back.add_argument("--no-resume", action="store_true")

    sub.add_parser("export", help="regenerovat docs/data.json z databaze")

    p_recl = sub.add_parser(
        "reclassify",
        help="preklasifikovat ulozene prilezitosti pomoci LLM (heuristic -> llm)")
    p_recl.add_argument("--limit", type=int, default=None,
                        help="zpracovat nejvyse N prilezitosti (default: vsechny)")
    p_recl.add_argument("--all", dest="force", action="store_true",
                        help="vzit i radky, ktere uz klasifikoval LLM")
    p_recl.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="jen spocitat kandidaty, nic nestahovat ani nezapisovat")

    p_soupis = sub.add_parser(
        "refresh-soupis",
        help="dohledat k ulozenym vecem nejnovejsi soupis majetkove podstaty "
             "a shortlist relevantnich dokumentu (jen HTTP, zadna PDF ani LLM)")
    p_soupis.add_argument("--limit", type=int, default=None,
                          help="zpracovat nejvyse N veci (default: vsechny)")
    p_soupis.add_argument("--all", dest="force", action="store_true",
                          help="vzit i veci, ktere uz soupis i dokumenty maji "
                               "(mohl pribyt novejsi soupis nebo dalsi dokument)")

    p_dos = sub.add_parser(
        "dossier",
        help="slozit podklad k jedne veci: syrovy text vsech relevantnich "
             "dokumentu do docs/dossier/<slug>.md (bez LLM)")
    p_dos.add_argument("spisova_znacka",
                       help="spisova znacka veci, napr. 'KSOS 37 INS 16018/2019' "
                            "(mezery a lomitka nejsou povinne)")
    p_dos.add_argument("--max-docs", dest="max_docs", type=int, default=dossier.MAX_DOCS,
                       help="nejvyse N dokumentu (default %d)" % dossier.MAX_DOCS)
    p_dos.add_argument("--stdout", action="store_true",
                       help="vypsat cely markdown i na standardni vystup")

    p_search = sub.add_parser("search", help="jen vypsat nalezene dluzniky (bez stahovani)")
    p_search.add_argument("--from", dest="date_from", type=_date,
                          default=date.today() - timedelta(days=10))
    p_search.add_argument("--to", dest="date_to", type=_date, default=date.today())

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if args.command == "daily":
        stats = pipeline.run_daily(days=args.days)
        if not args.no_export:
            pipeline.export_json()
        print(stats)
    elif args.command == "backfill":
        stats = pipeline.run_backfill(args.date_from, args.date_to,
                                      resume=not args.no_resume)
        pipeline.export_json()
        print(stats)
    elif args.command == "export":
        print(pipeline.export_json())
    elif args.command == "reclassify":
        stats = pipeline.run_reclassify(limit=args.limit, force=args.force,
                                        dry_run=args.dry_run)
        if not args.dry_run and not stats.get("aborted"):
            pipeline.export_json()
        print(stats)
        if stats.get("aborted"):
            return 1
    elif args.command == "refresh-soupis":
        stats = pipeline.refresh_case_docs(limit=args.limit, only_missing=not args.force)
        pipeline.export_json()
        print(stats)
    elif args.command == "dossier":
        try:
            markdown, stats = dossier.build_dossier(args.spisova_znacka,
                                                    max_docs=args.max_docs)
        except ValueError as exc:
            print("CHYBA: %s" % exc, file=sys.stderr)
            return 1
        # Export az po sestaveni - teprve ted existuje soubor, na ktery
        # export_json navesi "dossier_url" pro dashboard.
        pipeline.export_json()
        print(stats["path"])
        print(stats)
        if args.stdout:
            print()
            print(markdown)
    elif args.command == "search":
        records = search.search_window(args.date_from, args.date_to)
        for r in records:
            print("%-28s %-40s %s" % (r["spisova_znacka"],
                                      (r["dluznik_jmeno"] or "")[:40], r["stav_rizeni"]))
        print("celkem: %d" % len(records))
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
