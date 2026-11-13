---
name: bibliography-review
description: Workflow for reviewing and maintaining the site's bibliographic memory (_memory/) - which specialised agent/model to use for each step (sync, curation, audit), and which general plugin skills to reuse. Load when adding sources, resolving duplicates/DOIs, or before publishing the publications pages.
---

# Bibliography review workflow

State lives in `_memory/` (not published). Tool: `uv run _memory/tools/memory.py {sync,doi,metrics,import-cv,bib,index,search,stats}`. Conventions: `AGENTS.md` and `_memory/README.md`.

## Who does what (model chosen by the cost of being wrong)

| Step | Agent | Model | Why |
|---|---|---|---|
| Refresh from sources, run DOI/metrics, check invariants, report diffs | `bib-sync` | sonnet | Mechanical, deterministic commands; cheap |
| Resolve ambiguous items (duplicates, versions, homonyms, DOI candidates, missing years) and record decisions in `overrides.json` | `bib-curator` | **opus** | Judgement with incomplete evidence; wrong calls are published |
| Independent audit before generating public pages | `bib-auditor` | **opus** | Adversarial verification; must not share the curator's blind spots |

Run order for a normal update: `bib-sync` -> `bib-curator` (only if sync reports open items) -> `bib-sync` (re-run) -> `bib-auditor` -> generate pages. Never let the auditor and the curator be the same invocation.

## Reusable plugin skills/agents (research-superpowers, installed globally)
- `research-superpowers:literature-scout` / `literature-review`: discover works missing from ORCID/OpenAlex/Scholar (only on request; results are candidates, never auto-confirmed).
- `citation-needed`: verify factual claims in prose (bio, project descriptions) before publishing them.
- `research-superpowers:wiki-lint` is for wiki projects, not this memory.

## Rules that never change
- Owner's CV/ORCID/Scholar items are theirs; Scholar is the reference to accept/reject candidates (lenient before 2010).
- DOI is the uniqueness/truth key; no DOI -> title + authors + venue evidence.
- Never copy the CV file or personal data into the repo (it is public). Never commit `index.sqlite`.
- Opus agents write only `_memory/overrides.json`, with a one-line evidence note per decision; low-confidence cases become questions for the owner.
