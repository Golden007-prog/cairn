"""Expression objects → the ONE expression language (#1107 grammar).

The SDK never invents a second expression dialect: every ref renders to the
exact strings the record grammar already speaks — ``$input.<name>``,
``$steps.<id>.<path>``, and predicate strings like
``$steps.check.verdict == 'pass'``. Comparisons on refs build predicate
RECORDS, not booleans (ADR-0087 P1) — which is why ``__eq__`` here returns a
:class:`Predicate` and hashing is pinned back to identity.
"""
from __future__ import annotations

from typing import Any, Optional


def _lit(value: Any) -> str:
    """Render a Python literal the way the record grammar quotes it."""
    if isinstance(value, str):
        return "'" + value.replace("'", "\\'") + "'"
    if isinstance(value, bool):
        return "true" if value else "false"
    return repr(value)


class Ref:
    """Base for anything that renders to a ``$...`` reference string."""

    def _render(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    # ── comparisons build predicates, not booleans ──────────────────────────
    def __eq__(self, other: Any) -> "Predicate":  # type: ignore[override]
        return Predicate(self, "==", other)

    def __ne__(self, other: Any) -> "Predicate":  # type: ignore[override]
        return Predicate(self, "!=", other)

    def __ge__(self, other: Any) -> "Predicate":
        return Predicate(self, ">=", other)

    def __gt__(self, other: Any) -> "Predicate":
        return Predicate(self, ">", other)

    def __le__(self, other: Any) -> "Predicate":
        return Predicate(self, "<=", other)

    def __lt__(self, other: Any) -> "Predicate":
        return Predicate(self, "<", other)

    # defining __eq__ kills default hashing; refs are identity-hashed on purpose
    __hash__ = object.__hash__


class InputRef(Ref):
    """``inp.<name>`` → ``$input.<name>``."""

    def __init__(self, name: str) -> None:
        self.name = name

    def _render(self) -> str:
        return f"$input.{self.name}"

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<inp {self._render()}>"


class OutRef(Ref):
    """``<step>.out.<field>[...]`` → ``$steps.<id>.<dot.path>``.

    Carries the owning step id so the compiler derives the data edge (D4: data
    edges are the authoritative ordering truth).
    """

    def __init__(self, step_id_fn, path: tuple[str, ...]) -> None:
        # step ids are bound by __set_name__ AFTER refs may have been built
        # (class-body evaluation order), so the id is resolved lazily.
        self._step_id_fn = step_id_fn
        self._path = path

    @property
    def step_id(self) -> str:
        return self._step_id_fn()

    def __getattr__(self, name: str) -> "OutRef":
        if name.startswith("_"):
            raise AttributeError(name)
        return OutRef(self._step_id_fn, self._path + (name,))

    def _render(self) -> str:
        return f"$steps.{self.step_id}." + ".".join(self._path)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<out {self._render()}>"


class Predicate:
    """A comparison over refs — renders to the grammar's predicate string."""

    def __init__(self, left: Any, op: str, right: Any) -> None:
        self.left, self.op, self.right = left, op, right

    def _render_side(self, side: Any) -> str:
        return side._render() if isinstance(side, Ref) else _lit(side)

    def _render(self) -> str:
        return f"{self._render_side(self.left)} {self.op} {self._render_side(self.right)}"

    def ref_step_ids(self) -> list[str]:
        """Step ids this predicate reads — dependency edges for the compiler."""
        return [s.step_id for s in (self.left, self.right) if isinstance(s, OutRef)]

    __hash__ = object.__hash__


class _InputNamespace:
    """The ``inp`` singleton: attribute access mints :class:`InputRef`s."""

    def __getattr__(self, name: str) -> InputRef:
        if name.startswith("_"):
            raise AttributeError(name)
        return InputRef(name)


inp = _InputNamespace()


def render(value: Any) -> Any:
    """Project a wire value into record form: refs render to ``$...`` strings,
    predicates to predicate strings, plain literals pass through untouched."""
    if isinstance(value, (Ref, Predicate)):
        return value._render()
    if isinstance(value, dict):
        return {k: render(v) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v) for v in value]
    return value


def ref_steps(value: Any) -> list[str]:
    """Every step id a wire value reads — the D4 data edges."""
    if isinstance(value, OutRef):
        return [value.step_id]
    if isinstance(value, Predicate):
        return value.ref_step_ids()
    if isinstance(value, dict):
        return [s for v in value.values() for s in ref_steps(v)]
    if isinstance(value, list):
        return [s for v in value for s in ref_steps(v)]
    return []


__all__ = ["InputRef", "OutRef", "Predicate", "Ref", "inp", "ref_steps", "render"]
