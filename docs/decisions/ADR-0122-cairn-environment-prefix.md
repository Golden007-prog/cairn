# ADR-0122 — The CAIRN_* environment prefix, with OBS_* read until 1.0

**Status:** accepted (2026-10-13, #2402, epic #2399; amended by ADR-0123). D1 records the owner decision of
2026-10-11; D2–D5 need approval.

## Context
cairn's configuration lives in environment variables named for the product's former name:
188 distinct `OBS_*` names across code, compose files, Helm values and docs. The code reads
169 of them. `cairn/core/settings.py` is meant to be the single inventory (`cairn/CLAUDE.md`
rule 4), but it lists 37; 135 names the code reads are not in it (#2412). Publishing the core
puts these names in every user's deployment files, where renaming them later breaks working
installs.

## Decision
**D1 — The prefix is `CAIRN_`.** Every `OBS_<NAME>` becomes `CAIRN_<NAME>`. The rest of the
name stays the same.

**D2 — Both are read until 1.0.** `CAIRN_<NAME>` wins when both are set. `OBS_<NAME>` alone
still works, and is reported once per process as a deprecation warning that names the new
variable. Lookup goes through one function in `cairn/core/settings.py`; no module reads
`os.environ` for a cairn variable directly. Third-party names such as `OPENAI_API_KEY` and
`OTEL_*` are not affected.

**D3 — The inventory is complete first.** #2412 adds every variable the code reads to
`settings.py` (name, default, owning module, since). A test fails when code reads a cairn
variable that has no inventory row. The rename is mechanical over that list.

**D4 — Every surface moves at once.** Code, `deploy/` (compose files, `.env.example`, Helm
values and templates), workflows, docs and the console's build-time variables are renamed in
the same change (#2429). `cairn deploy doctor` reports `OBS_*` names in a `.env` or values
file as warnings, with the replacement name.

**D5 — `OBS_*` is removed at 1.0** (#2432). After that, an `OBS_*` variable is ignored, and
`cairn doctor` / `cairn deploy doctor` say so.

## Consequences
- An existing deployment keeps working through 0.x without edits and sees one warning per
  variable.
- The `secure`, `sandbox` and `prod` profiles are TOML, not environment variables, so they
  are not affected.
- The settings inventory becomes enforceable, which is the precondition `cairn/CLAUDE.md`
  rule 4 already states.

## Verification
The inventory test (#2412); a test that an `OBS_<NAME>` value is read with a warning and that
`CAIRN_<NAME>` wins when both are set; `cairn deploy doctor` on a `.env` that uses old names.
