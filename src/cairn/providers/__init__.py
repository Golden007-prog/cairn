"""cairn.providers — pluggable backend providers kept OUT of the core import
path and discovered via entry-point groups (e.g. ``cairn.vector_stores``).

Shipped in the monorepo (registry = monorepo) but selected/loaded only on
demand, with their heavy third-party deps behind optional extras. Core never
imports these directly.
"""
