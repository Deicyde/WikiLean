# Contributing to WikiLean

Thanks for considering a contribution. WikiLean is a small, focused project: an annotated mirror of Wikipedia's mathematics articles with each statement linked (where possible) to its formalization in Mathlib4. There are three independent ways to help.

---

## 1. Annotating on the live site

This is the easiest and highest-impact contribution path. **No setup needed beyond a browser and a GitHub account.**

### Sign in
1. Visit any article on [wikilean.jackmccarthy.org](https://wikilean.jackmccarthy.org) — e.g. [Prime ideal](https://wikilean.jackmccarthy.org/Prime_ideal).
2. Click the **✎ Sign in to edit** button bottom-right; auth is via GitHub OAuth.
3. After auth, the in-context editor loads on every article.

### The annotation model
Each annotation marks a span of article text with:
- **`kind`** — `definition` · `proposition` · `theorem` · `example`
- **`status`** — `formalized` · `partial` · `not_formalized`
- **`mathlib.decl`** + **`mathlib.module`** — the Mathlib declaration name and its module (when formalized)
- **`note`** — a one-sentence explanation
- **`anchor`** — the article span being annotated (managed automatically by the editor)

### What to do
- **Edit an existing annotation:** click any green/yellow/red highlight, change the fields, hit Save. Your edit is stored as a new revision; the previous state is kept in history.
- **Add a new annotation:** select text in an article, click **+ Annotate**, fill in the panel, Save. The selection becomes the new annotation's anchor.
- **Mark something correctly-formalized:** if Mathlib4 has a matching declaration, set the decl (e.g. `Ideal.IsPrime`) and module (`Mathlib.RingTheory.Ideal.Prime`). The editor links out to the Mathlib docs so you can verify.
- **Flag a wrong AI guess:** the seed annotations are AI-generated. Many are right; some are not. If a `decl` doesn't actually formalize the statement, fix it or change the status.

### Quality bar
- Citations should resolve: if you write `Mathlib.SetTheory.ZFC` and `IsPrime`, the docs link generated from that should open the right page.
- Be honest about partial matches: `partial` means "Mathlib has the building blocks but no statement of this exact result." `not_formalized` is a fine answer.
- One annotation = one statement. Don't merge unrelated claims into a single anchor.

### What happens to your edits
- Saved instantly to the production D1 database; visible to the next reader immediately.
- Edits are kept in `revisions` and can be viewed at `https://wikilean.jackmccarthy.org/<slug>/history`.
- The seeding pipeline that pushes new AI annotations from the local repo to D1 is **edit-safe**: it explicitly skips any article that has a user revision. So your work is never silently overwritten by an automated pass.
- Occasional moderation passes (an AI moderation agent reviewing edits in context) preserve `provenance: "human"` annotations verbatim — see `site/batch_annotate.py`'s moderation mode.

---

## 2. Code contributions

The repo is three loosely-coupled pieces. Pick the one matching your interest.

### `site/` — the annotation pipeline (Python)
- `render.py` — fetches Wikipedia HTML and renders the annotated page.
- `batch_annotate.py` — orchestrates the 2-agent pipeline (enumeration + Mathlib matching), with a `--moderate` mode that preserves human edits.
- `export_w3c.py`, `export_wikidata_rdf.py` — exports to standards-aligned formats.
- `serve_review.py` — the local review server (predecessor of the live wiki editor).

To run locally:
```sh
cd site
python3 -m venv ../catalog/.venv
source ../catalog/.venv/bin/activate
pip install -r requirements.txt   # if present; otherwise: requests claude-agent-sdk
python3 render.py Prime_ideal     # render a single article
```

### `catalog/` — the article catalog (Python)
- `fetch_catalog.py` — enumerates WikiProject Math from the MediaWiki API.
- `tag_with_mathlib.py` — parallel AI agents that grep Mathlib for each concept.
- `build_concept_layer.py`, `export_wikidata_rdf.py` — the concept (QID-level) layer + RDF.

See [catalog/README.md](catalog/README.md) for full details.

### `wiki/` — the live wiki backend (TypeScript)
- Cloudflare Worker, Hono framework, Drizzle ORM, D1, KV, R2.
- See [wiki/README.md](wiki/README.md) for setup and deploy instructions.

### Pull requests
- Branch from `main`, push, open a PR against `Deicyde/WikiLean`.
- For substantive changes, open an issue first to discuss scope.
- Tests: `cd wiki && npm test` for the Worker side; the Python side has no test suite yet, contributions welcome.

---

## 3. Upstream contributions

WikiLean is partly a bet that math-formalization metadata belongs on Wikidata and in Mathlib itself.

### Wikidata
- A property proposal for **"Mathlib declaration"** (an external identifier mapping a math concept to its primary Mathlib4 declaration) is drafted at [docs/wikidata_property_proposal.md](docs/wikidata_property_proposal.md). Once posted on Wikidata, support comments and concrete feedback on the identifier-format question both help.

### Mathlib4
- Mathlib4 has an `@[wikidata]` attribute (defined in `Mathlib/Tactic/CrossRefAttribute.lean`) that tags declarations with their Wikidata QID — the *inverse* of the proposed property. The WikiLean concept layer is being used to propose batch additions of this tag.

---

## Data & research notice

WikiLean is, in part, an experiment in **human + AI database moderation**. Like Wikipedia, edits are public and attributed: your GitHub display name, the article you edited, the timestamp, and your edit comment are all public.

Beyond the visible edit, we may analyze edit *metadata* — which fields changed, whether a change was AI- or human-authored (provenance), and timing — and publish findings **in aggregate** to study how humans and AI moderate a shared database together. Research exports use salted-hash pseudonyms by default and never include emails or IP addresses. If you edit, you're contributing to that record; that's the whole point, and we'd rather you know up front.

## Donating compute (token donations)

> **Planned / not yet live.** This describes the intended path, not a current feature. Today the annotation pipeline runs from the maintainer's machine.

WikiLean's annotation and moderation pipeline is AI-driven, and the plan is that **anyone will be able to donate compute by running the pipeline locally** on their own Anthropic API key or Claude subscription. The results (new/updated annotations) get reviewed and seeded into the wiki the same way maintainer-run passes are.

A few commitments on this, stated now so the policy is clear:

- **WikiLean will never collect or store your API keys or subscription credentials.** Not in the database, not in logs, not anywhere.
- The planned "donate a key" path is a **GitHub Actions template you fork**: the key lives only in *your* fork's repository secrets, and the pipeline runs in *your* Actions, against *your* account.
- **You cap your own spend** in your own Anthropic console. We can't bill you and can't see your usage.

## Code of conduct

Be kind, be precise about math, prefer evidence over assertion. Substantive disagreements are good; condescending ones are not.

## License recap

By contributing you agree your contributions are licensed as: code → MIT, annotation data → CC0. The article text on the site remains CC BY-SA from the upstream Wikipedia source.
