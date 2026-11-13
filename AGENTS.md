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
Estat inicial: les col·leccions `_publications`, `_talks`, `_teaching`, `_posts` contenen encara **exemples de la plantilla**; només `_pages/about.md`, `_pages/cv.md` i l'autor a `_config.yml` són reals.
Pla:
1. Recopilar la informació real (bio, CV, publicacions, docència, projectes) de les fonts existents.
2. Guardar-la com a **contingut estructurat en memòria dels agents** (font de veritat del contingut, versionada al repo).
3. Construir/regenerar la web a partir d'aquesta font. El motor (Academic Pages/Jekyll) es pot mantenir o canviar: decisió oberta, es discutirà amb l'usuari.

## Workflow de disseny
Es treballa **via screenshots**: l'agent aixeca la web a Podman, captura pantalles i les ensenya a l'usuari per validar la direcció abans d'avançar. Iterar sobre el feedback; no desplegar res fins que l'usuari ho demani.

## Convencions
- Comunicació amb l'usuari: **català**.
- Commits i PRs: **anglès**, imperatiu. Branques temporals `feature/`, `fix/`, `docs/`, `chore/` des de `develop`.
- Canvis quirúrgics; el que no es demana no es toca.
