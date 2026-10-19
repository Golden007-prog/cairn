"""Shared data-validation services (ADR-0074).

The tier-1 loader + tier-2/3a profiler + tier-3b quality engine, in the shared
services layer so both the `data/profile` builtin and the `data/expectations`
official operator (and the run pre-flight + Playground upload) reuse them.
"""
from cairn.services.data.loaders import (
    cell,
    detect_dataset_format,
    detect_format,
    load_or_rows,
    load_records,
)
from cairn.services.data.profile import (
    DataExpectation,
    DataProfile,
    Role,
    RoleReport,
    profile_data,
)
from cairn.services.data.quality import (
    Expectation,
    ExpectationReport,
    ExpectationResult,
    ExpectationSuite,
    run_expectations,
)

__all__ = [
    "load_records", "load_or_rows", "detect_format", "detect_dataset_format", "cell",
    "DataExpectation", "Role", "RoleReport", "DataProfile", "profile_data",
    "Expectation", "ExpectationSuite", "ExpectationResult", "ExpectationReport", "run_expectations",
]
