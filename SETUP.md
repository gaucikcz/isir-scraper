# Nastaveni repozitare (jednorazove, ~10 minut)

Kroky delej v tomto poradi. Body 1, 2 a 4 jsou povinne, bez nich to nepobezi.

---

## 1. Zaloz repozitar na GitHubu a nahraj kod

Na GitHubu vpravo nahore **+** -> **New repository**:

- **Repository name:** `isir-scraper`
- **Public** nebo **Private** (Pages funguji i u privatniho repa, ale jen na placenem planu -
  pokud mas Free, zvol **Public**)
- **NEZASKRTAVEJ** "Add a README file", "Add .gitignore" ani "Choose a license" -
  repozitar musi zustat prazdny
- **Create repository**

Pak v terminalu v korenu projektu (`~/isir-scraper`):

```bash
cd ~/isir-scraper
git add .
git commit -m "ISIR scraper: prvni verze"
git branch -M main
git remote add origin https://github.com/<user>/isir-scraper.git
git push -u origin main
```

`<user>` nahrad svym GitHub uzivatelskym jmenem. Pokud uz `origin` existuje, pouzij
`git remote set-url origin https://github.com/<user>/isir-scraper.git`.

---

## 2. Zapni GitHub Pages (dashboard)

V repozitari: **Settings** -> v levem menu **Pages** -> sekce **Build and deployment**:

- **Source:** `Deploy from a branch`
- **Branch:** `main` a slozka `/docs` -> **Save**

Za 1-2 minuty (obnov stranku) se nahore objevi adresa dashboardu. Tvar je vzdy:

```
https://<user>.github.io/<repo>/
```

tedy napr. `https://<user>.github.io/isir-scraper/`

Dokud neprobehne prvni sber, muze byt stranka prazdna. Spust si rucne beh podle bodu 5.

---

## 3. ANTHROPIC_API_KEY (VOLITELNE)

**Settings** -> v levem menu **Secrets and variables** -> **Actions** -> zalozka **Secrets**
-> tlacitko **New repository secret**:

- **Name:** `ANTHROPIC_API_KEY`
- **Secret:** klic z console.anthropic.com
- **Add secret**

Co se stane, kdyz klic nenastavis: nic se nerozbije. Sber, stahovani PDF i dashboard bezi
dal, ale klasifikace majetku se dela heuristikou podle klicovych slov misto LLM. Prakticky
to znamena hrubsi popisy majetku (`popis`), casteji chybejici `navrhovana_cena` a `kupujici`,
slabsi `shrnuti` a mene spolehlive rozdeleni `nemovity` / `movity` / `nehmotny`, tedy i
mene presnou prioritu. V datech to poznas podle `"classified_by": "heuristic"`.

Klic muzes doplnit kdykoli pozdeji, dalsi beh uz pojede s LLM.

---

## 4. Prava pro zapis (POVINNE)

Workflow commituje databazi a `docs/data.json` zpet do repozitare. Bez tohoto nastaveni
kazdy denni beh spadne na chybe pri `git push`.

**Settings** -> v levem menu **Actions** -> **General** -> sekce **Workflow permissions**:

- vyber **Read and write permissions**
- **Save**

---

## 5. Rucni spusteni (i z mobilu)

Zalozka **Actions** -> v levem sloupci **ISIR denni sber** -> vpravo tlacitko
**Run workflow** -> pole **Kolik dni zpetne prohledat** (vychozi `10`, muzes prepsat treba na `30`)
-> zelene **Run workflow**.

Jinak bezi automaticky kazdy den v **04:17 UTC** = 06:17 letni cas / 05:17 zimni cas.

Po dobehnuti klikni na beh a uvidis souhrn (kolik novych prilezitosti, kolik s vysokou
prioritou) primo v GitHub aplikaci, bez otevirani dashboardu.

Historicky dobeh minulych let: stejne misto, workflow **ISIR historicky backfill**,
vyplnis **Datum OD** (napr. `2024-01-01`) a volitelne **Datum DO**. Bezi hodiny -
je zamerne rate-limitovany na ~1 pozadavek za sekundu, at nezatezuje justice.cz.

---

## 6. Ikona na plose iPhonu

V **Safari** (ne v Chrome - ten to neumi) otevri `https://<user>.github.io/isir-scraper/`,
dole klepni na ikonu **Sdilet** (ctverec se sipkou) -> sjed dolu -> **Přidat na plochu**
-> **Přidat**. Dashboard se pak otevira na celou obrazovku jako aplikace.
