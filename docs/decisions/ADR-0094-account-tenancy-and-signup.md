# ADR-0094 — Account tenancy and self-serve signup: bind the workspace, then open the door

Status: **PROPOSED** (2026-08-25 — epic #1564, gated on #1609).

Relates to / builds on: **ADR-0054** (ONE Account tenant, admin-invite, no
public signup — this ADR revisits the "no signup" half and keeps the rest),
**ADR-0051 §4** (workspace contract + query-enforced scoping — the filtering
discipline this relies on), **#392 p2 / #448** (connector account isolation —
the one boundary already enforced two-layer), **#1609** (the workspace is
self-asserted — the prerequisite), **ADR-0093** (console journey, which already
names "ADR-0054 kept, improved with invite links").

## 1. Context

### What already exists

The multi-tenant substrate is largely built and was never wired to a front
door. `cairn/core/account.py` ships `Account`, `Membership`, and roles
(`owner` / `admin` / `member`, with `ADMIN_ROLES` gating the invite API). Users
carry an `account_id`; the JWT carries it as a claim; `caller_account()`
resolves it per request; `ConnectionStore` filters every read and delete on it,
with the API denying separately (the two-layer isolation #448 shipped).

What is missing is only the way in: ADR-0054 chose admin-invite and no public
signup, and the console login page says so in as many words — *"No account? Ask
your instance admin."*

### What an empirical audit found (2026-08-25)

Probing a running engine — create a resource in workspace A, list it from
workspace B — the *filtering* is good and consistent. Runs, audit, series,
judges, knowledge corpora, triggers, datasets, pipelines and installed
workflows are all correctly workspace-scoped. Audit is scoped end-to-end: the
sink stamps `workspace_id` at write, the reader filters at read.

But **a workspace is a filing mechanism, not a security boundary** (#1609).
`_WorkspaceMiddleware` takes the workspace from the URL or the
`X-Cairn-Workspace` header and sets it active with no membership check, no
account check, no authorization at all. `Workspace` has no `account_id` — it is
not bound to a tenant — and `GET /workspaces` returns every workspace in the
deployment, unfiltered and (unlike its POST/DELETE/PATCH siblings)
unauthenticated.

Under ADR-0054 that is defensible: one account, admin-invite, every user is one
company's staff, and the threat model is colleagues. `Workspace`'s own
docstring concedes the gap — *"A tenancy boundary. Minimal day-one entity; RBAC
/ membership / …"*.

Self-serve signup removes that assumption. Two unrelated companies then share a
deployment, and company B reads company A's audit trail with one header.

## 2. Decision

### D1 — Account is the company; workspace is a governed unit inside it

An **Account** is the tenant a human signs up as: a company. A **Workspace** is
a governed unit of work within it — a team, a project, an environment. Many
workspaces per account.

This is not new architecture; it is what the existing scoping already assumes.
The split of what is scoped where is the evidence, and it is a good split:

| level | holds | why |
|---|---|---|
| **Account** | connections / credentials, provider bindings, members + roles | configured once, expensive to duplicate, company-wide by nature |
| **Workspace** | runs, audit, series budgets, installed workflows, datasets, pipelines, triggers | the governed work, and the unit people want separate budgets and separate audit for |

It buys the thing the product is for: *"the fraud team has its own budget, its
own audit trail and its own enabled workflows — on the company's shared
provider key."*

### D2 — One email, one account now; the seam for many later

A user belongs to exactly one account today. The flexibility is bought by one
rule: **`account_id` is resolved per request from a membership lookup, never
read as the source of truth from the token.**

Today that lookup returns exactly one account. Multi-account later becomes
"return more than one, plus an account switcher" — no token format change, no
re-issued credentials, no migration of live sessions. The `account_id` JWT
claim stays as a hint and a fast path; it stops being authoritative.

### D3 — Bind and authorize the workspace FIRST (#1609)

Before any signup route ships:

1. `Workspace` gains `account_id`, stamped at create from the caller's account.
2. The middleware authorizes the asserted workspace against the caller's
   account and **404s** on a mismatch — not 403, matching the existing
   convention that cross-tenant existence must not leak (ADR-0051 §4, the same
   choice `_run_in_ws_or_404` already makes).
3. `GET /workspaces` is auth-gated and account-filtered.

This is wiring an existing boundary into the workspace path, not inventing
tenancy.

### D4 — One authorization function, not a scattered check

The workspace check is **one function every surface calls**, never a block each
router copies. Account-level membership is what it enforces today: every member
of an account may use every workspace in it.

That is deliberately simple and deliberately insufficient for later — a company
where finance must not see the ML team's audit trail needs per-workspace ACLs.
Putting the decision in one function makes that a change to one function
instead of to every call site.

This is the reachability lesson from #1376/#1402 restated: a chokepoint must be
a FUNCTION others call, not a BLOCK others copy.

### D5 — Three signup modes, one knob, closed by default

`OBS_SIGNUP` selects the front door:

- **`invite`** — **the default.** Nobody self-serves; joining is by invite link
  only. Correct for a self-hosted box holding someone's audit trail and
  credentials: a deployment must never accumulate strangers' accounts because
  somebody found the URL.
- **`domain`** — anyone with a work email matching the account's verified
  domain self-serves into *that* account. The expected steady state for real
  teams: it removes the admin from the loop without opening the door, and it is
  what the "company name + work email" signup shape is for.
- **`open`** — anyone creates an account. For a hosted demo/cloud deployment
  only.

Defaulting to `invite` means an existing deployment that upgrades gains no new
attack surface until an operator opts in.

### D6 — First-run bootstrap owns the first account

On a deployment with no accounts, the first person to complete signup becomes
`owner` of a new account, regardless of mode. This is how self-hosted MLflow
and Langfuse behave, and it removes the bootstrapping paradox (an invite-only
system with nobody to send the first invite).

Once any account exists, the mode governs.

### D7 — Invite links, not admin-set passwords

The existing admin API creates a user with a password the *admin* chooses,
which is both awkward and poor practice. An invite is a signed, expiring,
single-use token in a link; the invitee sets their own password and lands in
the inviting account with the role the inviter picked.

### D8 — The console extends `.login-A`; it does not invent page chrome

Signup, invite-accept and the "check your email"/error states are the same
surface as login and must look it. `console/src/styles/cairn.css` already has a
scoped `.login-A` block — card, logo, `.field`, `.lbl`, `.inp`, `.pri`, `.foot`
— deliberately namespaced so auth styling cannot collide with the gallery's
identically-named classes.

New auth screens reuse those class names inside `.login-A`. Specifically:

- no new colors — `var(--brand)`, `var(--line)`, `var(--muted)`, `var(--ink)`,
  `var(--panel2)`, `var(--brand-wash)` as the login card already uses;
- no inline `style={{}}` — the repo has a lint gate against it
  (`npm run lint:styles`), and every new rule belongs in `cairn.css`;
- the login footer changes from *"No account? Ask your instance admin."* to a
  real link when — and only when — the mode allows self-serve. Under
  `OBS_SIGNUP=invite` it keeps today's honest message rather than offering a
  door that will refuse.

Design mockups, if any are drawn, go in `design/*.html` next to the existing
ones and feed `cairn.css` — the established direction (#179).

## 3. Alternatives rejected

**Signup first, boundary second.** Rejected outright. It ships a front door
onto a building whose internal doors do not lock: the audit trail, the very
artifact this product sells, would be readable across companies with one
header. #1609 is not a prerequisite for tidiness; it is what makes signup safe.

**Account per workspace (flatten the model).** Simpler to authorize, but it
forces every team to re-enter the company's provider credentials, and it makes
"one company, several projects, one bill" unexpressible. The account-scoped
connection work (#448) would have to be undone.

**Workspace-level membership now.** More correct, more expensive, and not yet
asked for. D4 makes it additive rather than a rewrite, which is the right
trade while the shape of the demand is unknown.

**`account_id` authoritative in the JWT.** Simplest, and it is what exists
today — but it makes multi-account a token-format migration with live sessions
to invalidate. D2 costs one lookup per request and keeps that door open.

**Open signup by default.** Wrong default for self-hosted. An operator who
upgrades should not silently gain a public registration page on a box holding
their governance records.

## 4. Consequences

- **Sequencing is fixed**: #1609 (bind + authorize) → account-scoped workspace
  listing → invite links → signup modes → console screens. Each step is safe to
  ship alone; the order is not negotiable.
- **Existing single-account deployments are unaffected.** `OBS_SIGNUP` defaults
  to `invite`; the implicit `default` account and workspace keep working; the
  per-request account lookup returns the same single account it does today.
- **A new per-request lookup** joins the hot path. It is a membership read,
  cacheable per request, and it replaces reading a claim — measure before
  optimizing, but expect it to be noise next to a run.
- **ADR-0054 is amended, not superseded**: ONE account per user, admin-invite
  and the role model all stand. Only "no public signup" is revisited, and only
  behind a default-closed knob.
- **#1609 is load-bearing for the epic.** Until it lands, every downstream
  ticket here is unsafe to deploy on a shared instance.
