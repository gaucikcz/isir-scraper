# Náklady na LLM klasifikaci (ISIR scraper)

Model `claude-sonnet-5`, ceník **$2,00 / 1M vstupních tokenů**, **$10,00 / 1M výstupních
tokenů**. Kurz **21 CZK/USD** (přibližný, kurz si přepočítejte podle skutečnosti).

Měřeno 2026-09-05 nad 33 skutečnými PDF v `data/pdf/`. **Bez jediného volání API.**

---

## 1. Stav databáze, ze kterého se počítá

| Zdroj | Počet záznamů | `classified_by='llm'` | `classified_by='heuristic'` |
|---|---:|---:|---:|
| `data/isir.sqlite3`, tabulka `opportunities` | 103 | **27** | **76** |
| `docs/data.json` (okno 2026-07-22 – 2026-09-04) | 103 | 27 | 76 |
| `docs/data.sample.json` (ukázková data v repu) | 4 | 3 | 1 |

Sedí to se zadáním: 103 příležitostí, z toho 76 heuristických (ty čekají na
překlasifikaci) a 27 už klasifikovaných LLM. Ceny níže proto počítám pro 76, 27
a 103 dokumentů.

---

## 2. Co se posílá na jedno volání

Na **každém** volání jde do vstupu tohle (nic z toho se nikde neušetří):

| Část requestu | Znaků | Tokenů (tiktoken cl100k) | Odhad pro Claude |
|---|---:|---:|---:|
| `SYSTEM_PROMPT` | 2 278 | 1 001 | 1 150 – 1 350 |
| `TOOL_SCHEMA` (definice nástroje, posílá se pokaždé) | 1 201 | 384 | 440 – 520 |
| **Fixní režie celkem** | **3 479** | **1 385** | **1 600 – 1 900** |
| Uživatelská zpráva (hlavička případu + text dokumentu) | 410 – 24 295 | 159 – 12 711 | 180 – 17 200 |

Uživatelská zpráva = 5 řádků metadat případu + text z `pdftotext`/OCR **oříznutý na
`MAX_INPUT_CHARS = 24000` znaků** + patička. Strop 24 000 znaků je to jediné, co drží
horní hranici ceny — 6 z 31 dokumentů na něj skutečně narazilo.

Vstupní tokeny na jeden dokument (fixní režie + zpráva), odhad pro Claude:

| | tiktoken cl100k | odhad Claude |
|---|---:|---:|
| minimum | 1 544 | 1 800 – 2 100 |
| medián (typický dokument) | 4 936 | **5 700 – 6 700** |
| průměr (včetně dlouhých) | 6 258 | **7 200 – 8 400** |
| maximum (oříznutý dokument) | 14 096 | 16 200 – 19 000 |

**Fixní režie (system + tool schema) je 22 % průměrného vstupu** (medián 28 %, u
nejkratších dokumentů až 90 %). To je jediná položka, kterou by mělo smysl cachovat
nebo zkrátit, kdyby objem výrazně narostl — viz sekce 5.

Výstup: model vrací jeden blok `tool_use` (assets + spravce + shrnuti). Změřeno na
všech **27 skutečných LLM výsledcích** v databázi, serializovaných do stejného JSONu,
jaký model odeslal: 349–3 076 znaků (medián 658, průměr 945), tedy při naměřených
2,30 znaku na token zhruba 152–1 337 tokenů cl100k (medián 286, průměr 411). Odhad
pro Claude: **330 – 390 tokenů u mediánového dokumentu, 470 – 555 u průměrného** —
průměr nahoru táhnou dlouhé návrhy s mnoha položkami. Strop `MAX_TOKENS = 2000` zatím
nepadl, ale rezerva je jen ~10 %: nejdelší z těch 27 odpovědí vychází na horní
hranici odhadu na ~1 800 tokenů. Kdyby ho návrh s opravdu dlouhým soupisem
překročil, blok `tool_use` se ořízne, `classify_document()` chybu spolkne a tiše
spadne zpátky na heuristiku — stálo by to peníze a vrátilo horší výsledek.

Kód posílá `thinking: {"type": "disabled"}`, které Sonnet 5 přijímá, takže se
neplatí žádné thinking tokeny.

---

## 3. Cena

| Položka | USD | CZK (à 21) |
|---|---:|---:|
| **Typický (mediánový) dokument** | **$0,015 – $0,017** | **0,31 – 0,36 Kč** |
| **Průměrný dokument** (včetně dlouhých) | **$0,019 – $0,022** | **0,40 – 0,47 Kč** |
| Nejdražší možná kombinace (vstup oříznutý na 24k + nejdelší viděný výstup) | $0,048 – $0,056 | 1,0 – 1,2 Kč |
| Běh 27 dokumentů *(ty už LLM klasifikoval)* | $0,52 – $0,60 | 11 – 13 Kč |
| **Překlasifikace 76 heuristických dokumentů** | **$1,45 – $1,70** | **30 – 36 Kč** |
| Vše dohromady (103 dokumentů) | $1,97 – $2,30 | 41 – 48 Kč |
| **Ustálený provoz, 3 podání/den** | **$0,057 – $0,067 / den** | **1,2 – 1,4 Kč / den** |
| **Ustálený provoz, měsíčně (~91 dokumentů)** | **$1,74 – $2,03** | **36 – 43 Kč** |
| Ustálený provoz, ročně (~1 095 dokumentů) | $21 – $24 | 440 – 515 Kč |

Pro rychlý odhad: **zhruba 2 US centy, tedy ~0,45 Kč za dokument.**

Žádnou slevu za dokumenty bez textu tu nepočítám: 2 z 33 PDF sice lokálně nevydala
žádný text (`classify_document()` je pošle rovnou do heuristiky a nestojí nic), ale
jen proto, že tady chybí `tesseract`. V CI se OCR nainstaluje a proběhne, takže tyhle
dokumenty reálné peníze stát budou — viz sekce 6.

---

## 4. Metoda

Text jsem z každého ze 33 PDF vytáhl přímo funkcí `isir.extract.extract_text()` a
zprávu složil zavoláním skutečné `isir.classify._build_user_message()` s reálnými
metadaty případu z SQLite — tedy stejným kódem, který běží v produkci, ne jeho
napodobeninou. Změřil jsem znaky. **Klíč k API k dispozici není, takže jsem nemohl
použít `/v1/messages/count_tokens` — jediný přesný způsob, jak zjistit počet tokenů
pro Claude.** Jako náhradu jsem lokálně tokenizoval knihovnou `tiktoken` (kódování
`cl100k_base`). Dokumentace Anthropic výslovně říká, že tiktoken pro Claude použít
**nemá**: podceňuje počet tokenů zhruba o 15–20 % u běžného textu a víc u textu
jiného než anglického. Proto neuvádím jedno „přesné“ číslo, ale pásmo — naměřený
tiktoken počet × 1,15 až × 1,35. Naměřený poměr byl 2,30 znaku na token (cl100k),
tedy výrazně horší než u angličtiny; může za to čeština, ale hlavně výstup
`pdftotext -layout` plný čísel, tabulek a odsazení. Odtud pochází celá šíře pásma.

---

## 5. Cachování se tu neuplatňuje

Kód nikde nenastavuje `cache_control`, takže **žádná sleva za cache se neděje** —
každý dokument je samostatné jednorázové volání s jinou uživatelskou zprávou.

Doplnění oproti zadání: minimální cachovatelný prefix je u Sonnetu 5 **1024 tokenů** a
naše fixní část (system + tool schema) má odhadem 1 600–1 900 tokenů, takže na hranici
technicky **dosáhne** — cachovat by šlo. Ekonomicky to ale zatím nemá smysl, a to i po
započtení toho, co se často zapomíná: **zápis do cache stojí 1,25× cenu vstupu, čtení
0,1×, a výchozí životnost záznamu je 5 minut.** Kdyby se z cache četlo pokaždé, byl by
strop úspory ~15 % ceny volání. Při 3 dokumentech denně (jeden denní běh je zpracuje
hned za sebou, do 5 minut) ale jedno volání cache zapisuje a jen dvě z ní čtou, takže
reálná úspora vychází na **~9 %**, tj. asi **3 – 4 Kč měsíčně**. Za to nestojí zásah do
kódu. Kdyby objem vzrostl o řád, je to první věc, kterou udělat (fixní režie je 22 %
vstupu).

---

## 6. Co je měřené a co je odhad

**Měřené (tvrdá data, reprodukovatelná bez sítě):**
- Přesná délka `SYSTEM_PROMPT` (2 278 znaků) a `TOOL_SCHEMA` (1 201 znaků).
- Přesná délka uživatelské zprávy pro každé z 33 reálných PDF, sestavená produkčním kódem.
- Kolik dokumentů narazí na strop 24 000 znaků (6 z 31) a kolik nevydá text vůbec (2 z 33).
- Velikost výstupní JSON struktury z reálně uložených výsledků.
- Ceník $2 / $10 za milion tokenů pro `claude-sonnet-5`.

**Odhadované (může se lišit, u vstupu odhaduji chybu do ±20 %):**
- Převod znaků na tokeny. Tokenizér Claude je jiný než tiktoken a nemám ho lokálně.
- **Vstup a výstup jsou měřené na dvou různých skupinách dokumentů.** Z PDF ležících
  v `data/pdf/` nepatří ani jedno k některému z 27 LLM řádků — ty klasifikovalo CI a
  jejich PDF se sem nikdy nestáhla (`data/pdf/` je v `.gitignore`). Průnik je nulový,
  takže žádný řádek v sekci 3 není cena jednoho konkrétního doběhlého volání: je to
  průměrný vstup jedné skupiny dokumentů spárovaný s průměrným výstupem druhé.
- Velikost výstupu skutečného modelu. Odvozena z 27 uložených LLM výsledků, ne
  z reálných hlaviček `usage` z API.
- Předpoklad 3 nových podání denně — vzatý ze zadání, ne z dat. Databáze pokrývá
  2026-07-22 – 2026-09-04, tedy 45 dní a 103 příležitostí = **~2,3 denně**; řádky
  „ustálený provoz" jsou proto spíš horní odhad.

**Známé zkreslení směrem dolů:** lokálně chybí `tesseract`, takže dvě naskenovaná PDF
tady nevydala text. V CI (`.github/workflows/*.yml`) se instaluje
`tesseract-ocr tesseract-ocr-ces`, takže tam se OCR provede (až 8 stran) a takový
dokument bude stát reálné peníze, ne nulu. U skenů jsou tedy moje čísla podhodnocená.

---

## 7. Kde vidíte skutečné číslo

Odhad výše nikdy nenahradí účtenku. Skutečnou spotřebu najdete na
**console.anthropic.com → Usage** (tokeny po dnech, modelech a workspace) a
**→ Cost** (peníze). Filtrujte podle workspace, který používá `ANTHROPIC_WORKSPACE_ID`
v GitHub Actions. Po jakémkoli dalším běhu si tam porovnejte skutečnost s tabulkou
v sekci 3 — pár dní reálných dat je víc než celý tenhle dokument.
