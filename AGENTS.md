# AGENTS.md — Web personal (xbaro.eu)

Font única de veritat per a tots els agents. `CLAUDE.md` només importa aquest fitxer (`@AGENTS.md`); no duplicar contingut allà.

## Què és
Web personal de Xavier Baró, publicada a https://xbaro.eu.
Repositori: `xbaro/xbaro.github.io` (públic). Basada en la plantilla **Academic Pages** (Jekyll, derivada de Minimal Mistakes). La primera versió es va fer amb Claude Design.

## Branques i desplegament
- `master` = producció. GitHub Pages amb workflow (`.github/workflows/jekyll.yml`, Ruby 3.1): cada push a `master` desplega. Domini `xbaro.eu` configurat a Settings → Pages (no hi ha `CNAME` al repo).
- **`develop` = branca de treball** (creada des de `master` el 2026-11-13). No es desplega.
- Mai push directe a `master`. Els canvis arriben a `master` només amb PR autoritzat per l'usuari.
- Repo públic: no afegir claus, dades privades ni contingut no destinat a ser públic.

## Estructura
- `_config.yml` — configuració (títol, autor, analítiques, cookies).
- `_pages/` — pàgines (about, cv, publications, talks, teaching, cookie-policy, terms...).
- `_publications/`, `_talks/`, `_teaching/`, `_portfolio/`, `_posts/` — col·leccions.
- `_data/` — navegació i textos de la interfície.
- `files/` — PDFs descarregables. `images/`, `assets/`, `_sass/`, `_includes/`, `_layouts/` — tema i recursos.
- `markdown_generator/` — scripts per generar publicacions/xerrades des d'un TSV.

## Entorn de proves: Podman (obligatori)
Totes les proves i previsualitzacions s'executen a **Podman** (Podman Desktop, màquina `podman-ta-dev`), mai instal·lant Ruby/Jekyll al sistema.
```bash
podman build -t xbaro-web -f Dockerfile .
podman run -d --name xbaro-web -p 4000:4000 -v "$(pwd)":/usr/src/app:Z xbaro-web sh -c \
 'bundle exec jekyll build -c _config.yml,_config.dev.yml && (bundle exec jekyll build -w --force_polling -c _config.yml,_config.dev.yml >/tmp/watch.log 2>&1 & exec ruby -run -e httpd _site -p 4000 -b 0.0.0.0)'
# http://localhost:4000 · aturar: podman rm -f xbaro-web
```
Per què no `jekyll serve`: en `development` Jekyll sobreescriu `url` amb `http://0.0.0.0:4000` i Chrome bloqueja aquests recursos (pàgina en blanc). `_config.dev.yml` fixa `url: http://localhost:4000` només per a la previsualització local; no afecta producció.

Screenshots (Chrome headless, sense Playwright): `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --hide-scrollbars --virtual-time-budget=8000 --window-size=1280,1000 --screenshot=<fitxer>.png http://localhost:4000/` (sense `--virtual-time-budget` la captura surt en blanc).
Hi ha altres contenidors d'altres projectes en marxa a la mateixa màquina: no tocar-los, no reutilitzar els seus ports (5432, 6379, 3900, 3903, 8123); usar `4000`.

## Python: sempre `uv`
Qualsevol script Python (generador de contingut, scripts de recopilació) usa un entorn gestionat amb **`uv`** (`uv venv`, `uv pip install`, `uv run`). Mai `pip` global ni `python3 -m venv` manual.

## Contingut i memòria dels agents
Estat inicial: les col·leccions `_publications`, `_talks`, `_teaching`, `_posts` contenen encara **exemples de la plantilla**; només `_pages/about.md` i l'autor a `_config.yml` són reals. **`_pages/cv.md` també és plantilla** («GitHub University»); el CV real encara no és al repo.
**Memòria estructurada: `_memory/`** (dins el repo, però **no es publica**: Jekyll ignora les carpetes que comencen per `_`; verificat, `/_memory/...` dona 404). El repo és públic: només informació ja pública.
- `_memory/references.json` (font de veritat, generat), `_memory/overrides.json` (correccions manuals que sobreviuen a cada sync), `_memory/sources/` (cache ORCID/OpenAlex), `_memory/index.sqlite` (cerca FTS5, derivat, fora de git), `_memory/tools/memory.py` (CLI amb `uv`). Detalls a `_memory/README.md`.
- **CV de l'usuari** (format AQU, desembre 2024): font de confiança. `uv run _memory/tools/memory.py import-cv <CV.docx>` n'extreu només publicacions i xerrades (`sources/cv_*.json`). **Mai copiar al repo (públic) ni el .docx ni dades personals** (DNI, adreça, naixement...).
- **Google Scholar** de l'usuari: export BibTeX manual a `_memory/sources/scholar.bib` (Select all → Export → BibTeX); es fusiona com a font de confiança en `sync --offline`.
- **DOI = clau d'unicitat i veracitat**: `uv run _memory/tools/memory.py doi` (verifica contra Crossref/DataCite i cerca DOIs que falten), després `sync --offline`. Detalls a `_memory/README.md`.
- Estats: `confirmed` (a l'ORCID, al CV o a Scholar de l'usuari), `candidate` (només a OpenAlex, per revisar), `duplicate` (versió preprint d'una altra; `duplicate_of`), `rejected` (homònims; no es tornen a proposar).
- `_memory/references.bib`: BibTeX canònic de les confirmades, es manté al repo per facilitar l'exportació (regenerat per `sync`/`bib`).
- Mètriques (cites Crossref/OpenAlex, indicadors de revista OpenAlex, quartils SCImago amb CSV manual): `uv run _memory/tools/memory.py metrics` i després `sync --offline`. IF oficial (JCR) és de pagament: només a mà a `overrides.json`. Detalls a `_memory/README.md`.
- Actualitzar: `uv run _memory/tools/memory.py sync` · cercar: `... search "<text>"` · resum: `... stats`.
- Fonts: ORCID `0000-0001-5338-3007` + OpenAlex (APIs públiques, sense clau; paginar amb `page=`, el `cursor=` exigeix clau).
- **Criteri per acceptar/rebutjar candidates** (decisió de l'usuari): el seu perfil de Google Scholar (`user=N-rA1NMAAAAJ`) és la referència. Si una obra no surt a Scholar, probablement no és seva → `rejected`. Per a obres **anteriors al 2009-2010** es pot ser més laxe (poden faltar a Scholar). Scholar bloqueja l'accés automatitzat (429/CAPTCHA, no s'ha d'evitar): cal que l'usuari n'exporti el perfil (Select all → Export → BibTeX/CSV) i el deixi al repo.
- **Portal de la Recerca de Catalunya** (https://portalrecerca.csuc.cat/orcid/0000-0001-5338-3007, perfil únic `rp20011`; `prc.cat` no existeix): integrat a `sync`/`portal` via OpenSearch Atom; aporta `repository_url` a cada obra i les tesis a `_memory/theses.json` (4 co-dirigides + la pròpia del 2009; el CV no té secció de direcció de tesis). **Compte amb l'homònim Xavier Baró Queralt** (historiador, ~130 ítems al portal): importar només per `author_authority:rp20011`.

Pla:
1. Recopilar la informació real (publicacions fet; falten bio, CV, docència, xerrades, projectes).
2. Guardar-la a `_memory/` (font de veritat del contingut).
3. Generar les pàgines Jekyll (`_publications/`, etc.) a partir de la memòria. Es manté Academic Pages/Jekyll de moment (decisió de l'usuari); el motor es revisarà més endavant.

## Agents i models per a la revisió bibliogràfica
Definits a `.claude/agents/` (no es publiquen: Jekyll ignora els directoris amb punt) i flux a la skill `.claude/skills/bibliography-review/`.

| Agent | Model | Quan |
|---|---|---|
| `bib-sync` | sonnet | Refresc mecànic (`sync`/`doi`/`metrics`/`bib`), invariants i informe de diferències |
| `bib-curator` | **opus** | Casos ambigus (duplicats, versions, homònims, DOIs dubtosos, anys): decideix i escriu només `_memory/overrides.json` amb evidència |
| `bib-auditor` | **opus** | Auditoria independent i només-lectura abans de generar pàgines públiques |

Ordre: `bib-sync` → `bib-curator` (si queden ítems oberts) → `bib-sync` → `bib-auditor` → generar pàgines. Opus només on equivocar-se es publica; la resta, Sonnet. Per descobrir obres que falten: `research-superpowers:literature-scout` (només a petició; resultats = candidates). Per verificar afirmacions de la bio: skill `citation-needed`.

## Workflow de disseny
Es treballa **via screenshots**: l'agent aixeca la web a Podman, captura pantalles i les ensenya a l'usuari per validar la direcció abans d'avançar. Iterar sobre el feedback; no desplegar res fins que l'usuari ho demani.

## Convencions
- Comunicació amb l'usuari: **català**.
- Commits i PRs: **anglès**, imperatiu. Branques temporals `feature/`, `fix/`, `docs/`, `chore/` des de `develop`.
- Canvis quirúrgics; el que no es demana no es toca.
