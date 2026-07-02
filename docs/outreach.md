# Collaboration outreach — DRAFTS for Jack to review & send

> These are **drafts**. They are outward-facing messages to named people; the agent
> never sends them. Edit to taste, then send yourself. Each is designed to open with a
> concrete artifact WikiLean already has (the gift), not an ask. Numbers verified
> 2026-07-02 against the repo — re-check before sending if the data has moved.

Shared framing: WikiLean is the QID-keyed layer the formal-math ecosystem is missing —
every concept joined to Wikidata (the only cross-database hub in math), with a
human-moderation loop (propose-then-approve) and now a multilayer atlas. The three
projects below each hold a layer we lack, and lack a layer we hold.

---

## 1 — Vasily Ilyin (Theorem Graph / theoremsearch.com) · vilin@uw.edu

**The gift:** the concept (Wikidata-QID) layer Theorem Graph doesn't have, built from
*your own* extraction sources.

> Subject: A Wikidata concept layer for Theorem Graph
>
> Hi Vasily,
>
> I've been studying the Theorem Graph paper (arXiv 2606.25363) closely while building
> WikiLean (wikilean.jackmccarthy.org) — an annotated mirror of Wikipedia's math
> articles where every statement is joined to its Mathlib declaration through a shared
> Wikidata QID. Your formal↔informal matching is exactly the literature layer I don't
> have; the QID concept layer is exactly the one you note you don't have (no
> deduplication of cross-paper restatements, no concept identity). They compose.
>
> Two concrete things I can hand you today:
>
> 1. A **decl → QID table** for ~820 Mathlib declarations (984 QID pairs), so your
>    `statement_formal` rows get a concept column — the key that says "these two
>    restatements are the same theorem."
> 2. A **Formal-Conjectures → QID mapping** (342 statements → 114 Wikidata items),
>    derived from the same FC extraction your graph already ingests. FC's repo has zero
>    Wikidata references; nobody has built this, and it's a clean merge into your table.
>
> Both are CC0. In the other direction, your judged formal↔informal matches are the
> literature edges WikiLean's graph is missing — I'd love to ingest the
> precision-labelled slice (deterministic / high-cosine) through our human-review queue.
>
> One thing I need to sort before building on the matching set: the HuggingFace card
> says CC-BY-SA-4.0 and the paper says CC-BY-NC-SA — could you confirm which governs the
> matches? The NC clause changes whether WikiLean can republish.
>
> Happy to share the tables and talk architecture whenever suits.
>
> — Jack

---

## 2 — Chris Birkbeck (LMFDB / LeanBridge) · [Zulip / email]

**The gift:** an independent cross-check of LeanBridge's `mathlib=` knowls, from a
separately-curated QID↔Mathlib fabric — plus the missing piece for LMFDB objects.

> Subject: Cross-checking LeanBridge's Mathlib knowls + the LMFDB↔Wikidata gap
>
> Hi Chris,
>
> I saw your `nf.degree_mathlib_def` knowl and then found the broader LeanBridge effort —
> it's great, and close to something I've been building from the other side. WikiLean
> keys Mathlib declarations to Wikidata QIDs; I now run a live resolver
> (wikilean.jackmccarthy.org/decl/Nat.Prime → the mathlib4 docs) and I've just seeded
> the "Mathlib Declaration ID" Wikidata property (P14534).
>
> Because our two mappings were curated independently, agreement between them is real
> evidence and disagreement is worth a look. I'd like to run the triangle
> QID → LMFDB-knowl (P12987) → your `mathlib=` decl  vs  QID → decl (WikiLean) over your
> ~62 Mathlib knowls and send you a short consistency report — free QA in both
> directions.
>
> Separately, a gap I think is worth closing: Wikidata has a property for LMFDB *knowls*
> (P12987, ~109 uses) but none for LMFDB **object labels** — `11.a2`, `2.0.4.1`,
> `1.12.a.a` have no hub identity, so ~15 Wikipedia pages that cite lmfdb.org can't link
> structurally. I've just been through the property-proposal process for P14534 and would
> be glad to draft an "LMFDB label" property with you (verified per-label via API
> round-trip + invariant agreement — conductor/discriminant/q-expansion), which would
> make every famous curve and form a first-class node in the shared graph.
>
> Would a call be useful? I can bring the cross-check report to it.
>
> — Jack

---

## 3 — Pieter Belmans (Stacks Project / Gerby infrastructure) · [email]

**The gift:** the exact tag→decl artifact the stalled Nov-2024 plan needed to ship
Stacks → Mathlib backlinks.

> Subject: Reviving the Stacks → Mathlib tag backlinks
>
> Hi Pieter,
>
> I came across the Zulip thread from last November about adding Mathlib links to Stacks
> tag pages — icons in the ToC, a per-tag link fed by an hourly tag→decl dictionary. It
> seems to have stalled short of shipping, and I think I can supply the missing piece.
>
> Mathlib currently carries **323 distinct `@[stacks]` tags on 467 declarations** (same
> CrossRefAttribute machinery as the `@[wikidata]` tags). I can generate and keep current
> the tag → fully-qualified-decl JSON dictionary your plan called for, exported CC0, with
> each entry validated against the doc-gen4 declaration index so a redirect never rots.
> WikiLean already runs this style of nightly export for its own data.
>
> If useful, there's a second, lighter-weight opportunity: Wikidata has no "Stacks
> Project tag" property at all (the structured-data field for Q25099801 is entirely
> empty), even though nLab and Wikipedia cite tags constantly. I've just shepherded a
> Mathlib-declaration property through the proposal process and would be happy to propose
> a Stacks-tag one — your permanent-never-reused-tombstoned tag policy is close to ideal
> external-identifier material.
>
> Glad to send a sample of the tag→decl export whenever you'd like to see the shape.
>
> — Jack

---

## 4 — Mathlib maintainers (Lean/Mathlib Zulip, #general) — a shared Mix'n'match catalog

**The gift:** a ready-to-import, CC0 catalog of concept → Mathlib-declaration mappings, so
the crowd (not any one of us) can confirm them into Wikidata — the exact pattern LMFDB
already runs, reused for Mathlib instead of rebuilt.

**Why Mix'n'match, in one paragraph.** Mix'n'match (Magnus Manske's Wikidata tool) turns a
flat list of external-database entries into a public matching game: contributors confirm
"this entry = this Wikidata item," and each confirmation is written straight to Wikidata as
a statement on the external-identifier property. It needs exactly two things to exist first:
(1) that external-ID property — for us that's **Mathlib Declaration ID (P14534)**, now live —
and (2) a seed list. LMFDB already does this: **catalog 6447 syncs to LMFDB knowl ID
(P12987)**, ~1,246 knowls with ~109 confirmed onto Wikidata so far. Same machinery, a
different property, is all a Mathlib catalog is.

**What I can hand over today (CC0, no ask attached):** the WikiLean *join fabric* —
**1,025 (Wikidata-QID → Mathlib-decl) mappings** over **864 distinct declarations**, each row
carrying a provenance tier so you can trust-filter before importing:

| tier | rows | what it means |
|---|---|---|
| `merged` | 108 | the `@[wikidata]` tag is reviewed *and merged into mathlib master* (≥2 human reviewers incl. a maintainer + CI) |
| `mathlib-maintainer` | 150 | drawn from mathlib's own `docs/1000.yaml` |
| `ai-verified` | 61 | catalog mapping, second-pass checked |
| `ai` | 706 | catalog mapping, candidate only |

So **258 rows are already maintainer-blessed** (a clean pre-confirmed import) and ~767 are
candidates the crowd would vet in the game. It's a single diffable file (the same
"tag→decl dictionary" shape Mathlib already accepted for `@[stacks]`), rebuilt nightly, and
it round-trips through WikiLean's live `/decl/$1` resolver so a decl name never rots to the
wrong module.

> Subject: A community-matching catalog for Mathlib ↔ Wikidata (reusing LMFDB's setup)
>
> Hi all,
>
> The *Mathlib Declaration ID* property (P14534) recently went live on Wikidata. I'd like to
> stand up a Mix'n'match catalog against it — the same crowd-matching tool LMFDB already uses
> (catalog 6447 → LMFDB knowl ID, P12987) — so anyone can confirm which Wikidata concept a
> Mathlib declaration formalizes, one click at a time. I'll build and maintain it; I'm asking
> for a blessing and a little eyeballing, not labour.
>
> The seed already exists and is CC0: a 1,025-row (QID → decl) table over 864 declarations, with
> a provenance tier on every row. 258 of those are already maintainer-grade — 108 are
> `@[wikidata]` tags that reviewers and CI merged into master, 150 come from your own
> `docs/1000.yaml` — so that slice imports pre-confirmed, and the remaining ~767 candidates
> become a matching game the community can chip away at (each confirmation writes a P14534
> statement; each rejection is recorded, so nobody re-checks the same pair twice).
>
> Why it composes with what you already have:
> - It's **crowd-sourced curation of the concept↔decl map**, off the mathlib4 git path entirely —
>   no PRs, no CI load. The `@[wikidata]` tag pipeline stays exactly as is; this just gives the
>   *rest* of the mapping a public place to be verified.
> - **LMFDB is heading into Mathlib too** (the LeanBridge `mathlib=` knowls). If both databases
>   confirm against the same Wikidata hub with the same tool, "is this the same object?" becomes a
>   join instead of an argument — shared infrastructure rather than two parallel silos, which is
>   where I'd want to take it, with the LMFDB folks as the natural first partner.
> - Every confirmed row makes P14534 more useful to *everyone* downstream (search, doc backlinks,
>   the 1000-theorems list), not just WikiLean.
>
> What I'd want from you: a sanity check that a Mix'n'match catalog synced to P14534 is welcome and
> not stepping on anything, and a pointer if there's a Wikidata-side convention for Mathlib I should
> follow. I'll handle the import, the nightly refresh, and the scraper wiring. (If a maintainer later
> wants to bulk-confirm the 258 already-blessed rows to give the catalog a trustworthy core, that's a
> bonus — not something I'm asking for.)
>
> Happy to post the seed file and a screenshot of the catalog shape first if you'd rather see it
> before it's public.
>
> — Jack
