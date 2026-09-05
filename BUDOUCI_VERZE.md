# v2 — oslovit správce dřív, než se sami rozhodnou prodávat

**Stav: odloženo.** Vrátit se k tomu, až bude v1 (denní sledování I_347 + dashboard
+ dossier) fungovat k plné spokojenosti.

## Myšlenka

Současná v1 sleduje `I_347` — návrh správce soudu na zpeněžení mimo dražbu.
V tu chvíli už je správce rozhodnutý prodávat a někdy má i vybraného kupce.
Okno je sice off-market, ale ne nejranější možné.

v2 by mělo zachytit řízení **dřív**: ve chvíli, kdy je zřejmé, že majetková
podstata obsahuje nemovitost a řízení směřuje ke zpeněžení, ale správce ještě
nepodal návrh. Tehdy ho lze oslovit s nabídkou jako první.

## Co bude potřeba promyslet

- **Který signál sledovat.** Kandidáti: prohlášení konkursu, soupis majetkové
  podstaty (`I_334`/`I_454`/`I_538`), výpis z katastru nemovitostí (`I_829`),
  zprávy správce o stavu řízení. Pozor: podle měření na 20 řízeních s vysokou
  prioritou je soupis jen ve 30 % případů a katastr ve 25 % — samotný jeden
  signál nestačí.
- **Objem.** I_347 dává ~3 podání denně. Širší signál (např. každý nový konkurs
  s nemovitostí) může dávat řádově víc — bude potřeba ostřejší filtr, jinak se
  dashboard zahltí a klasifikace zdraží.
- **Načasování oslovení.** Příliš brzy = správce ještě nemá mandát prodávat
  (před prohlášením konkursu / schválením oddlužení nelze).
- **Forma oslovení.** Kontakty na správce se sbírají už ve v1. Zvážit datovou
  schránku vs. e-mail, a jestli generovat návrh dopisu, nebo jen podklad.
- **Etika a právo.** Ověřit, že aktivní oslovování správce s nabídkou na odkup
  není v rozporu s insolvenčním zákonem ani s postavením správce.

## Co už je z v1 použitelné

Kontakty na správce (jméno, e-mail, telefon, datová schránka) se extrahují
a ukládají už teď, právě kvůli této fázi — dokumenty se nebudou muset číst znovu.
