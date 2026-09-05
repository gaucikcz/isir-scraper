# Ověření metodiky proti živému ISIR (5. 9. 2026)

Vše níže bylo změřeno na produkčním `isir.justice.cz`, ne odvozeno z dokumentace.

## 1. Vyhledávací filtr nevrací jen „I_347 v okně" — DŮLEŽITÁ OPRAVA

Metodika (kap. 3) předpokládá, že `druh_kod_udalost=I_347` + `datum_akce_od/do`
vrátí dlužníky, u kterých v tom okně proběhla akce I_347. **Neplatí to přesně.**

Měření na okně 25. 8. – 5. 9. 2026:

| | počet |
|---|---|
| dlužníků vrácených vyhledáváním | 37 |
| z toho se **skutečnou** akcí I_347 v okně | **27** |
| falešně pozitivních | 10 |

Příklad falešné pozitivity: `KSOS 25 INS 21401/2013` (O.K.D.C. mont s.r.o.) má
13 akcí I_347, ale všechny z let 2014–2021. Jediná událost v okně je
**I_535 „Usnesení o prodeji mimo dražbu" z 4. 9. 2026**. Vyhledávání tedy vrací
věci, které kód události mají *kdykoliv v historii* a zároveň mají v okně
*nějaký* pohyb.

**Důsledek pro implementaci:** výsledek vyhledávání se NESMÍ brát jako seznam
nových podání. Slouží jen jako *kandidátní* seznam; autoritativní je až datum
z detailu věci. `pipeline.process_case()` proto filtruje
`date_from <= událost.datum <= date_to` proti oddílu B a 10 kandidátů zahodí.
Cena za to je 37 requestů na detail místo 27 — zanedbatelné.

## 2. Kód události je na stránce detailu strojově čitelný

Řádek oddílu B obsahuje skrytou buňku s kódem (`I_347`, `I_535`, …).
Parsujeme podle **kódu**, ne podle českého názvu akce — je to odolnější vůči
změnám formulace i vůči diakritice.

## 3. Události bez dokumentu reálně existují

Potvrzeno živě: `KSBR 47 INS 12093/2020` (Agency worker company, s.r.o.),
akce I_347 z 1. 9. 2026 nemá odkaz na PDF. Pipeline ji přeskočí a započítá do
`no_document`, nepadá.

## 4. Objem odpovídá odhadu

27 skutečných podání za 11 dní ≈ **2,5 podání denně** napříč celou ČR.
Metodika odhadovala 3–4/den. Denní běh s oknem 10 dní je tedy bohatě
dimenzovaný a díky dedup klíči `doc_id` nic nezpracuje dvakrát.

## 5. Ostatní body metodiky potvrzeny

- `pageB=all` vrátí celou historii jedním requestem (ověřeno: 650 událostí).
- Přímé PDF odkazy fungují bez session/cookie.
- Číselné ID v `dokument.PDF?id=…` je stabilní a slouží jako dedup klíč.
- `pdftotext -layout` extrahuje čitelný text bez jediného LLM tokenu.
- Cross-reference na „Soupis majetkové podstaty" funguje: k poslednímu I_347
  se dohledal soupis z 13. 8. 2026 (`doc_id 70480133`).
- U fyzických osob se rodné číslo skutečně zobrazuje (`505523/286` u jednoho
  z nálezů) — v databázi je proto ukládáno jen jako SHA-256.
