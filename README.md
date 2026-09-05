# ISIR scraper — hlídač prodejů majetkové podstaty mimo dražbu

Denní monitoring insolvenčního rejstříku (ISIR) na událost **I_347 — „Návrh na vydání souhlasu se zpeněžením
majetkové podstaty mimo dražbu"**. Systém stáhne PDF návrhu, vytáhne z něj text, roztřídí majetek, uloží ho
do SQLite a publikuje mobilní dashboard na GitHub Pages.

## Proč zrovna tohle

Když insolvenční správce podá návrh I_347, znamená to: **majetek se bude prodávat mimo dražbu, soud o tom
teprve rozhoduje, a kupec často ještě není vybraný.** To je to nejužší okno, kdy se dá do transakce vstoupit
— majetková podstata je identifikovaná, správce je motivovaný prodat, ale smlouva ještě neexistuje.

Proto nesledujeme:

| Kód | Co znamená | Proč ne |
|---|---|---|
| **I_347** | Návrh na souhlas se zpeněžením mimo dražbu | **Toto sledujeme** — okno je otevřené |
| I_535 | Usnesení o prodeji mimo dražbu | Pozdější souhlas soudu — okno se zavírá |
| I_1028 | Smlouva o prodeji mimo dražbu | Smlouva už existuje = příležitost je pryč |

I_535 a I_1028 jsou ale užitečné jako **kontrolní signál pro pozdější fázi**: podle nich se dá u konkrétní
spisové značky ověřit, jestli se obchod mezitím uzavřel, za kolik a s kým. To je součást roadmapy, ne
současného běhu.

## Architektura

```
search      vysledek_lustrace.do?druh_kod_udalost=I_347   (max 30denní okno)
   |
   v
detail      evidence_upadcu_detail.do?id=<uuid>&actSheet=B&pageB=all
   |
   v
PDF         dokument.PDF?id=<numeric>          <- numerické id = klíč pro deduplikaci
   |
   v
text        pdftotext -layout   (fallback OCR: tesseract -l ces)
   |
   v
klasifikace Claude  (fallback: heuristika)  ->  assets[], priorita, správce, shrnutí
   |
   v
SQLite (data/isir.sqlite3)  ->  docs/data.json  ->  GitHub Pages dashboard
```

### Priorita

| Priorita | Podmínka |
|---|---|
| `vysoka` | jakékoli aktivum `typ="nemovity"` (pozemek, budova, byt, hala, areál) |
| `stredni` | jinak jakékoli `typ="movity"` (stroje, vozidla, zásoby, technologie) |
| `nizka` | jen `typ="nehmotny"` (pohledávky, podíly, ochranné známky, licence) |

## Moduly

| Soubor | Co dělá |
|---|---|
| `isir/config.py` | Konstanty: BASE URL, User-Agent, cesty (DATA_DIR, DB_PATH, DOCS_DIR), `EVENT_CODE="I_347"`, `SOUPIS_CODES`, `REQUEST_DELAY` |
| `isir/http.py` | `get()` s retry a slušným zpožděním, `get_pdf(doc_id)` |
| `isir/search.py` | `search_window(date_from, date_to)` — lustrace I_347 v daném okně, vrací spisové značky a odkazy na detail |
| `isir/detail.py` | `fetch_events(detail_id)` — celý seznam událostí spisu (`pageB=all`) včetně `doc_id` dokumentů |
| `isir/extract.py` | `extract_text(pdf_bytes)` -> `(text, source)`, kde source je `pdftotext` / `ocr` / `none` |
| `isir/classify.py` | Klasifikace textu na `assets[]`, `priorita`, `spravce`, `shrnuti`; LLM s heuristickým fallbackem |
| `isir/db.py` | SQLite: `init_schema`, `upsert_case`, `have_doc` (dedup), `insert_event`, `insert_opportunity`, `all_opportunities` |
| `isir/pipeline.py` | Orchestrace denního běhu i backfillu |
| `cli.py` | Vstupní bod: `daily`, `backfill`, `export` |

## Lokální spuštění

```bash
# systémové závislosti (macOS)
brew install poppler tesseract tesseract-lang

# prostředí (Python 3.9+; v CI běží 3.11)
cd ~/isir-scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

```bash
python cli.py daily --days 10          # doběhne posledních 10 dní
python cli.py backfill --from 2024-01-01   # historie po 30denních oknech
python cli.py export                   # zapíše docs/data.json
python -m http.server -d docs 8000     # náhled dashboardu na http://localhost:8000
```

Klasifikace přes Claude potřebuje `ANTHROPIC_API_KEY` v prostředí. Bez klíče systém běží dál a použije
heuristiku — dashboard takové záznamy označí.

## Nasazení a provoz

Jednorázové nastavení repozitáře (GitHub Pages nad složkou `/docs`, volitelný `ANTHROPIC_API_KEY`,
práva pro zápis) je krok za krokem v [SETUP.md](SETUP.md). Pak běží dva workflowy:

| Workflow | Spouštění | Co dělá |
|---|---|---|
| `.github/workflows/daily.yml` — *ISIR denní sběr* | denně 04:17 UTC + ručně (vstup `days`, výchozí 10) | `cli.py daily` + `cli.py export`, commit `data/` a `docs/data.json` |
| `.github/workflows/backfill.yml` — *ISIR historický backfill* | výhradně ručně (`date_from`, volitelně `date_to`) | `cli.py backfill` po 30denních oknech; běh trvá hodiny |

## Náklady

Jediný token spend je **jedno volání klasifikátoru na každý nový dokument**. Celostátně přibývají zhruba
**3–4 nové návrhy I_347 denně**, takže provozní náklad je zanedbatelný. `pdftotext` i OCR běží lokálně a
nestojí nic. Backfill je jednorázově dražší úměrně počtu historických dokumentů.

## Zatížení serveru a etika

Insolvenční rejstřík je ze zákona veřejný (insolvenční zákon) a data se z něj čtou tak, jak je publikuje.
Přesto:

- popisný **User-Agent** s kontaktem, žádné maskování,
- **~1 požadavek za sekundu** (`REQUEST_DELAY`), žádná paralelizace,
- denní běh = několik desítek požadavků,
- **backfill je throttlovaný a pouští se mimo špičku**.

## GDPR

Část dlužníků jsou fyzické osoby (OSVČ, oddlužení) a rejstřík u nich zveřejňuje **rodné číslo**. Databáze
ukládá pouze **SHA-256 hash** rodného čísla (kvůli deduplikaci), nikdy plaintext, a do `docs/data.json` se
nedostane vůbec. Primární cíl systému jsou **firmy a nemovitosti**, ne fyzické osoby.

## Známá omezení

- **Datový filtr ISIR má strop 30 dní** — delší období se musí procházet po oknech (řeší `backfill`).
- Některé události **skutečně nemají dokument** („není k dispozici") — takové se přeskakují.
- Starší dokumenty (zhruba **před rokem 2020**) bývají skeny; jde na ně OCR, kvalita textu je horší.
- **LLM klasifikaci je před jakýmkoli krokem potřeba ověřit proti zdrojovému PDF.** Dashboard odděleně
  označuje záznamy klasifikované jen heuristikou (`classified_by: "heuristic"`).
- Popis majetku je v návrhu občas jen **odkaz na soupis majetkové podstaty** („položka č. 4 soupisu").
  V takovém případě se dohledává nejbližší předcházející dokument soupisu ve stejném spisu — ale ne vždy
  se to podaří, pak je popis obecný.

## Roadmapa

- **Fáze 1 (hotovo):** detekce I_347, extrakce, klasifikace, kontakty na správce, dashboard.
- **Fáze 2:** ocenění — katastr nemovitostí, odhad tržní ceny, porovnání s navrhovanou cenou.
- **Fáze 3:** vygenerování a odeslání nabídky správci. Kontakty (e-mail, telefon, datová schránka) se sbírají
  už ve fázi 1 právě proto, aby se kvůli nim nemusely dokumenty číst znovu.
