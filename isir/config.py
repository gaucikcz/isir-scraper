"""Konfigurace a konstanty."""
import os
from pathlib import Path

BASE = "https://isir.justice.cz"
SEARCH_URL = BASE + "/isir/ueu/vysledek_lustrace.do"
DETAIL_URL = BASE + "/isir/ueu/evidence_upadcu_detail.do"
DOC_URL = BASE + "/isir/doc/dokument.PDF"

# Popisny User-Agent - viz kap. 1 metodiky (etika / rate limiting).
USER_AGENT = os.environ.get(
    "ISIR_USER_AGENT", "isir-scraper/1.0 (+https://github.com/; lukas@klimchi.com)"
)

# Sledovana akce: navrh spravce na zpenezeni majetkove podstaty mimo drazbu.
EVENT_CODE = "I_347"

# Kody soupisu majetkove podstaty - fallback cross-reference (kap. 7).
SOUPIS_CODES = ("I_334", "I_454", "I_538")

# Kody signalizujici, ze prilezitost je uz pravdepodobne pryc / potvrzena.
CONFIRM_CODES = {"I_535": "usneseni_o_prodeji", "I_1028": "smlouva_o_prodeji"}

# Vyhledavaci filtr na datum akce je na strane ISIR omezen na 30 dni.
MAX_WINDOW_DAYS = 30

# Prodleva mezi requesty v sekundach (~1 req/s, kap. 1).
REQUEST_DELAY = float(os.environ.get("ISIR_DELAY", "1.0"))
REQUEST_TIMEOUT = float(os.environ.get("ISIR_TIMEOUT", "60"))
MAX_RETRIES = 4

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PDF_DIR = DATA_DIR / "pdf"
DB_PATH = DATA_DIR / "isir.sqlite3"
DOCS_DIR = ROOT / "docs"
DATA_JSON = DOCS_DIR / "data.json"

# Kolik radku chtit najednou (povolene hodnoty 50/100/200/300/400).
ROWS_AT_ONCE = 400
