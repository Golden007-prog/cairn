""":class:`StorageBackend` — the single injectable §24.4 centers on.

One object exposing every store the engine needs (``runs``, ``memory``,
``rag`` today; ``sessions`` / ``checkpoints`` / ``approvals`` / ``budgets``
land in P6). Built once at startup from env via :func:`build_storage`,
injected into Dispatcher / Runner / CLI / chat endpoint. Every consumer
reads its concern off the same object instead of calling per-store
factories.

Why a plain dataclass and not a Protocol: the StorageBackend isn't
duck-typed against — it's *constructed* by the engine, *injected* into
consumers. Consumers program against the per-store Protocols
(:class:`~cairn.core.storage.protocols.RunStore`,
:class:`~cairn.core.storage.protocols.MemoryStore`, etc.), which is
where polymorphism actually lives. The backend is just a typed bundle.

Adding a new store kind (P6: sessions/checkpoints/approvals/budgets):
  1. Add a Protocol in :mod:`cairn.core.storage.protocols`.
  2. Add an impl in the matching backend subpackage (sqlite/postgres/etc.).
  3. Add a ``default_<kind>_store()`` factory next to the impl.
  4. Add a field to :class:`StorageBackend` here.
  5. Extend :func:`build_storage` to populate it.

That's it — no consumer churn beyond pulling the new store off the
backend they already hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import Any

from cairn.core.storage.protocols import MemoryStore, RagStore, RunStore
from cairn.core.storage.rag_catalog import RagCatalogStore


@dataclass
class StorageBackend:
    """Typed bundle of every store the engine needs.

    Constructed once via :func:`cairn.core.storage.build_storage` from
    env, then injected into stateful consumers. Mutation after construction
    is allowed but unusual — tests may swap a store in place to install a
    fake; production code should not.
    """

    #: Run + event log (RunStore couples runs and events per §24.4 §24.4.3
    #: design call — subscribe-with-backlog naturally couples them).
    runs: RunStore

    #: Episodic memory store. Today Qdrant-backed via the legacy adapter;
    #: P4.8 lands sqlite-vec; P5 lands pgvector.
    memory: MemoryStore

    #: RAG store (hybrid retrieval + synthesis). Multi-collection by design
    #: per pack composition (§24.3).
    rag: RagStore

    #: RAG index catalog — what's indexed, under which content/build hash
    #: (ADR-0023). Backs incremental indexing; persistent (SQLite) by default.
    rag_catalog: RagCatalogStore

    # ── Config-family stores (ADR-0058 / #427) ───────────────────────────
    # The 4 fields above are the engine-data stores, built eagerly by
    # build_storage(). The config-family stores below are built LAZILY on first
    # access (cached_property), so a process only opens the pools it uses — the
    # worker never constructs the policy/oauth/account stores, the CLI never
    # constructs the trigger store, etc. Each delegates to the store's factory,
    # which owns the single `CAIRN_STORAGE` → postgres/sqlite routing. This is the
    # one DI injection point: consumers take `get_storage()` and read `.policy`
    # / `.connections` / … off it instead of importing scattered factories.
    #
    # The factory imports are LAZY (inside each property) to break import cycles
    # (these modules import back into cairn.core) and to keep the accessor
    # build-on-demand.

    @cached_property
    def policy(self) -> Any:
        from cairn.core.policy.store import default_policy_store  # lazy: cycle + on-demand
        return default_policy_store()

    @cached_property
    def secrets(self) -> Any:
        from cairn.core import secrets_store  # lazy: cycle + on-demand
        return secrets_store._local()

    @cached_property
    def oauth(self) -> Any:
        from cairn.core.oauth.store import default_token_store  # lazy: cycle + on-demand
        return default_token_store()

    @cached_property
    def schedules(self) -> Any:
        from cairn.core.storage.schedule_store import default_schedule_store  # lazy
        return default_schedule_store()

    @cached_property
    def triggers(self) -> Any:
        from cairn.core.storage.trigger_store import default_trigger_store  # lazy
        return default_trigger_store()

    @cached_property
    def trigger_inbox(self) -> Any:
        # Out of the CAIRN_STORAGE axis (NATS/sqlite via CAIRN_TRIGGER_INBOX) — the
        # accessor delegates to its own selector, ADR-0058 §4.
        from cairn.core.storage.trigger_inbox import get_trigger_inbox  # lazy
        return get_trigger_inbox()

    @cached_property
    def profiles(self) -> Any:
        from cairn.core.profile_store import default_profile_store  # lazy
        return default_profile_store()

    @cached_property
    def installs(self) -> Any:
        from cairn.core.registry_install import get_install_store  # lazy
        return get_install_store()

    @cached_property
    def connections(self) -> Any:
        from cairn.core.connections import default_connection_store  # lazy
        return default_connection_store()

    @cached_property
    def account(self) -> Any:
        from cairn.core.account import get_account_store  # lazy
        return get_account_store()

    @cached_property
    def workspace(self) -> Any:
        from cairn.core.workspace import get_workspace_store  # lazy
        return get_workspace_store()

    @cached_property
    def workflow_registry(self) -> Any:
        from cairn.core.workflow_registry import get_workflow_registry  # lazy
        return get_workflow_registry()

    @cached_property
    def remote_jobs(self) -> Any:
        from cairn.core.operator.job_store import get_remote_job_store  # lazy
        return get_remote_job_store()

    @cached_property
    def eval_ledger(self) -> Any:
        """Eval ledger — verdicts, case results, failure labels (ADR-0059 §D3,
        #244). Built lazily, same as the other config-family stores; routes
        through ``CAIRN_STORAGE`` (postgres) or the shared ``cairn.db`` (sqlite).

        A product store (ADR-0120 D3/D4, #2421) still here because core reads it:
        the public ``routers/runs.py`` (``/runs/{id}/diagnosis``, ``/evaluate``, the
        bundle's eval rows — wave 2c takes them to the platform), the console-only
        ``routers/insights.py`` and ``eval/diagnosis.py`` (the continuous-eval consumer
        fires the run hooks instead, and the showcase seed left with the platform,
        #2421 wave 2f). The platform's scoring hook writes through its own ledger
        (``cairn_platform.stores``), not this one."""
        from cairn.core.storage.eval_ledger import default_eval_ledger  # lazy
        return default_eval_ledger()

    # Future fields (placeholders documented here so consumers know what's
    # coming; not added until the corresponding stores land):
    #
    #   sessions:    SessionStore      # P6 — chat session state
    #   checkpoints: CheckpointStore   # P6 — LangGraph state for resume
    #   approvals:   ApprovalStore     # P6 — pending HITL decisions
    #   budgets:     BudgetStore       # P6 — per-pack/per-template spend
