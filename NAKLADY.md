# Náklady na LLM klasifikaci (ISIR scraper)

Model `claude-sonnet-5`, ceník **$2,00 / 1M vstupních tokenů**, **$10,00 / 1M výstupních
tokenů**. Kurz **21 CZK/USD** (přibližný, kurz si přepočítejte podle skutečnosti).

Měřeno 2026-09-05 nad 33 skutečnými PDF v `data/pdf/`. **Bez jediného volání API.**

---

## 1. Nesrovnalost, kterou je nutné zmínit hned na začátku

Zadání mluví o 103 příležitostech, z toho 76 heuristických a 27 klasifikovaných LLM.
**V repozitáři to tak není.** Skutečný stav:

| Zdroj | Počet záznamů | `classified_by='llm'` | `classified_by='heuristic'` |
|---|---:|---:|---:|
| `data/isir.sqlite3`, tabulka `opportunities` | 74 | **0** | 74 |
| `docs/data.json` (okno 2026-08-06 – 2026-09-04) | 74 | **0** | 74 |
| `docs/data.sample.json` (ukázková data v repu) | 4 | 3 | 1 |

V databázi ani v exportu **není jediný záznam klasifikovaný LLM**. Běh 27 dokumentů
proto nelze změřit ze skutečných dat — po žádném takovém běhu tu nezůstala stopa.
Číslo pro 27 a 76 dokumentů níže uvádím tak, jak bylo zadáno (jako násobek ceny za
jeden dokument), a vedle toho i číslo pro **74 dokumentů, což je skutečný obsah DB**.

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

**Fixní režie (system + tool schema) je 27 % průměrného vstupu** (medián 28 %, u
nejkratších dokumentů až 90 %). To je jediná položka, kterou by mělo smysl cachovat
nebo zkrátit, kdyby objem výrazně narostl — viz sekce 5.

Výstup: model vrací jeden blok `tool_use` (assets + spravce + shrnuti). Změřeno na
skutečných uložených výsledcích serializovaných do stejného JSONu, jaký by model
odeslal: 3 ukázkové LLM řádky 216–358 tokenů (cl100k), 74 heuristických řádků
136–586 tokenů. Odhad pro Claude: **250 – 370 tokenů**. Strop `MAX_TOKENS = 2000` se
ani zdaleka nevyužije. Kód posílá `thinking: {"type": "disabled"}`, které Sonnet 5
přijímá, takže se neplatí žádné thinking tokeny.

---

## 3. Cena

| Položka | USD | CZK (à 21) |
|---|---:|---:|
| **Typický (mediánový) dokument** | **$0,014 – $0,017** | **0,30 – 0,36 Kč** |
| **Průměrný dokument** (včetně dlouhých) | **$0,017 – $0,021** | **0,36 – 0,43 Kč** |
| Nejdražší možný dokument (oříznutý na 24k) |  $0,035 – $0,042 | 0,74 – 0,88 Kč |
| Běh 27 dokumentů | $0,47 – $0,56 | 10 – 12 Kč |
| Překlasifikace 76 dokumentů | $1,31 – $1,56 | 27 – 33 Kč |
| Překlasifikace 74 dokumentů *(skutečný stav DB)* | $1,27 – $1,52 | 27 – 32 Kč |
| Vše dohromady (103 dokumentů) | $1,77 – $2,12 | 37 – 44 Kč |
| **Ustálený provoz, 3 podání/den** | **$0,052 – $0,062 / den** | **1,1 – 1,3 Kč / den** |
| **Ustálený provoz, měsíčně (~91 dokumentů)** | **$1,57 – $1,88** | **33 – 39 Kč** |
| Ustálený provoz, ročně (~1 095 dokumentů) | $19 – $23 | 400 – 470 Kč |

Pro rychlý odhad: **zhruba 2 US centy, tedy ~0,40 Kč za dokument.**

Malá korekce směrem dolů: 2 z 33 PDF nevydají žádný text, `classify_document()` je
pošle rovnou do heuristiky a **nestojí nic**. Na dávku dokumentů tedy platíte zhruba
94 % výše uvedeného.

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
technicky **dosáhne** — cachovat by šlo. Ekonomicky to ale zatím nemá smysl: ušetřilo
by to zhruba **16 % ceny jednoho volání**, což je při 3 dokumentech denně asi
**6 Kč měsíčně**. Za tu úsporu nestojí zásah do kódu. Kdyby objem vzrostl o řád, je to
první věc, kterou udělat (fixní režie je 27 % vstupu).

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
- Velikost výstupu skutečného modelu. Odvozena z uložených výsledků a 3 ukázkových
  LLM řádků, ne z reálných hlaviček `usage` z API.
- Předpoklad 3 nových podání denně — vzatý ze zadání, ne z dat. Za posledních 30 dní
  přibylo 74 příležitostí, tj. **~2,5 denně**, což ten předpoklad zhruba potvrzuje.

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
