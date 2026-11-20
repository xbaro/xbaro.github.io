# AGENTS.md — Web personal (xbaro.eu)

Font única de veritat per a tots els agents que treballen **en aquest repositori**. `CLAUDE.md` només importa aquest fitxer (`@AGENTS.md`); no duplicar contingut allà. És autocontingut: descriu la web, com es veu i què es mesura. Decidir quines dades hi van, i com s'obtenen, es fa fora d'aquest repositori.

## Què és
Web personal de Xavier Baró, publicada a https://xbaro.eu.
Repositori: `xbaro/xbaro.github.io` (públic). Basada en la plantilla **Academic Pages** (Jekyll, derivada de Minimal Mistakes). La primera versió es va fer amb Claude Design. Tres idiomes (ca, en, es).

## Branques i desplegament
- `master` = producció. GitHub Pages amb workflow (`.github/workflows/jekyll.yml`, Ruby 3.1): cada push a `master` desplega. Domini `xbaro.eu` configurat a Settings → Pages (no hi ha `CNAME` al repo).
- **`develop` = branca de treball** (creada des de `master` el 2026-11-13). No es desplega.
- Mai push directe a `master`. Els canvis arriben a `master` només amb PR autoritzat per l'usuari.
- Repo públic: no afegir claus, dades privades ni contingut no destinat a ser públic. Aquest repositori només conté dades ja aprovades per publicar.

## Estructura
- `_config.yml` — configuració (títol, autor, analítiques, cookies). `_config.dev.yml` — només previsualització local.
- `_pages/` — pàgines (about, publications, projects, teaching, cookie-policy, terms…); `_pages/en/` i `_pages/es/` les traduccions.
- `_data/` — navegació, textos de la interfície (`i18n.yml`, `ui-text.yml`, `navigation.yml`) i **dades aprovades** (veure més avall).
- `_includes/`, `_layouts/`, `_sass/`, `assets/` — tema. `images/` (incloent `images/logos/`), `files/` — recursos i PDFs descarregables.
- `_publications/`, `_talks/`, `_teaching/`, `_portfolio/`, `_posts/` — col·leccions de la plantilla.
- `markdown_generator/` — scripts per generar publicacions/xerrades des d'un TSV.

## Dades de la web (`_data/*.json`, `files/publications.bib`)
Són **dades ja aprovades, generades fora d'aquest repositori**; no s'editen a mà (la propera publicació les sobreescriu). Si una dada és errònia, es corregeix a l'origen i es torna a publicar; aquí només es canvia com es mostra.
- `publications.json` — `bibkey`, `title`, `authors`, `venue`, `year`, `type`, `doi`, `url`, `isbn`, `label` (`preprint`), `oa` (`pdf`/`html`/`version`, si hi ha versió oberta). `files/publications.bib` és el BibTeX descarregable de les mateixes.
- `projects.json` — `title`, `kind` (competitive/contract/sgr…), `funder`, `reference`, `start`, `end`, `pis`. Els imports no s'hi publiquen.
- `teaching.json` — institució (amb `logo` si existeix a `images/logos/`) → programes → assignatures, amb noms per idioma i un únic rang de cursos (`period_label`, p. ex. `2012/13–2015/16`).

### Com es mostren
- **Docència:** una fila per institució i assignatura amb un sol rang de cursos; només el que ja s'ha impartit (res de planificat). Programes: grau primer, després per any d'inici i nom. Institucions en ordre fix (UB, UOC, Paris-Saclay, UAB).
- **Logotips d'institució:** PNG RGBA transparents en negre (o SVG) a `images/logos/`. Un logo amb fons blanc opac es veu com una caixa si el fons de la pàgina no és blanc pur. Si es canvia un logo, canviar-ne el nom de fitxer per saltar la memòria cau del navegador (Safari).
- **Publicacions:** llista amb enllaços PDF/HTML/DOI/URL i, si n'hi ha, la versió oberta; botó de BibTeX per article i descàrrega de tot el conjunt (`_includes/publications-list.html`).

## Indicadors (analítica)
Google Analytics 4 (propietat `xbaro.eu`, `G-ENN14SG31P`), **només després del consentiment de cookies** (`_includes/cookie-consent.html`; si no hi ha consentiment, `gtag` no existeix i no s'envia res).
- Esdeveniments: `bibtex_view`, `bibtex_copy`, `bibtex_download`, `bibtex_download_all`, `publication_link_click`.
- Paràmetres: `publication_title`, `bibkey`, `doi`; a `publication_link_click` també `link_type` (`pdf`/`html`/`doi`/`url`), `oa_version` i `link_url`. Estan definits a GA4 com a dimensions personalitzades d'esdeveniment.
- Qualsevol canvi de noms o paràmetres s'ha de coordinar amb GA4 (dimensions i informes); no canviar-los a la lleugera.

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

## Workflow de disseny
Es treballa **via screenshots**: l'agent aixeca la web a Podman, captura pantalles i les ensenya a l'usuari per validar la direcció abans d'avançar. Iterar sobre el feedback; no desplegar res fins que l'usuari ho demani.

## Convencions
- Comunicació amb l'usuari: **català correcte, mai castellà** (ni barrejat: res de «dime», «vale», «entonces»…). Vigilar els castellanismes, també en els títols dels subagents. Codi, commits i PRs en anglès.
- Commits i PRs: **anglès**, imperatiu. Branques temporals `feature/`, `fix/`, `docs/`, `chore/` des de `develop`.
- Canvis quirúrgics; el que no es demana no es toca.
- **Mai afirmar què exigeix una norma, guia o agència (RGPD, LSSI, AEPD, Llibre d'estil UB…) sense haver llegit el text exacte de la font en aquesta sessió**: cal URL i cita literal, o es diu «no verificat». Les preferències pròpies no es presenten com a norma. Els textos legals publicats es revisen amb la skill `citation-needed` abans del PR.

## Multidioma (ca / en / es)

- Sense connectors: carpetes per idioma. Idioma per defecte `ca` a l'arrel (`/teaching/`), els altres sota `/<lang>/` (`/en/teaching/`, `/es/teaching/`). Idiomes a `languages` i `default_lang` de `_config.yml`.
- Cada pàgina porta `lang` i `translation_key` (mateixa clau = traducció l'una de l'altra); `_includes/lang-switch.html` genera el selector a la capçalera.
- Textos del lloc a `_data/i18n.yml` (un bloc per idioma). Afegir un idioma = bloc a `i18n.yml` + carpeta `_pages/<lang>/` + codi a `languages`.
- Les dades de `_data/*.json` ja porten els noms resolts per idioma (`ca`/`en`/`es`); si falta una traducció es mostra el català.
- Docència: una fila per institució i assignatura amb els **períodes** de cursos (`2012/13–2015/16`), no una per curs. Les planificades es marquen `planificat`.
- Pendent: `hreflang` al `<head>`, traducció de la resta de pàgines (publicacions, tesis, projectes, CV) i de les cadenes de `ui-text.yml` (cal bloc `ca-ES`).

## Pàgina «Datasets» (`/events/`)
- Generada de `_data/events.json` (`memory.py site`): llista plana per any, amb una icona al web del dataset i una a l'article que el presenta (només articles on l'autor és coautor).
- `show_events: false` al front matter amaga la pestanya «Esdeveniments» (dades a `events.json`, HTML no generat). Posar-ho a `true` per activar les pestanyes; el títol de la pàgina passaria a «Esdeveniments i datasets».
