"""The Ledger schema contract, shared by the application and standalone tools.

This module is deliberately **dependency-free**: it imports only the Python
standard library and never imports ``bot``, ``aiosqlite``, Telegram code, or
application configuration. That is what lets a standalone maintenance script
(``python -m scripts.backup_db``) and the production startup path agree on one
definition of "what a version-N database must contain" without the script
pulling in a bot token, an event loop, or a third-party driver.

There is exactly one source of truth here:

* :data:`LATEST_SCHEMA_VERSION` — the version this checkout migrates to.
  ``bot.migrations.LATEST_VERSION`` is a compatibility alias for it.
* :data:`TABLE_INTRODUCED` — the version at which each table first appears.
* :func:`required_tables_for` — the tables a database stamped at a given
  version must contain. Both the startup verifier and the backup verifier call
  this, so a copy can never be certified against a different table list than the
  one the bot enforces.

A version number is an *input* to verification, not proof of correctness: a
corrupt, partially restored, or mis-stamped database must fail closed rather
than be adopted as a rollback point.
"""

from __future__ import annotations

# The version this checkout migrates a database to. Bump it (and register the new
# migration in ``bot.migrations._MIGRATIONS``) for every schema change.
LATEST_SCHEMA_VERSION = 9

# Version 0 is the pre-versioning legacy shape. It is *not* a certifiable schema
# version: those databases never stamped a version and their table/column shape
# is unknown, so generic verification rejects them and the migration preflight
# handles them through an explicit rehearsal path instead.
LEGACY_UNVERSIONED = 0

# The schema version at which each table first appears. A verifier requires a
# table only once the version it is checking has reached that introduction, so an
# intermediate-version database (a pre-migration rollback point, or a test
# pinning an older effective latest) is never asked for objects a later
# migration will add.
TABLE_INTRODUCED: dict[str, int] = {
    # v1 — baseline (extracted verbatim from the pre-migration DDL)
    "users": 1,
    "study_logs": 1,
    "gym_logs": 1,
    "diet_logs": 1,
    "foods": 1,
    "food_portions": 1,
    "recipes": 1,
    "recipe_ingredients": 1,
    "habits": 1,
    "habit_logs": 1,
    # v2–v8 — one table (or table group) per migration
    "mutation_receipts": 2,
    "user_settings": 3,
    "habit_activity_periods": 4,
    "reminder_deliveries": 5,
    "diet_log_items": 6,
    "user_food_preferences": 7,
    "catalog_foods": 8,
    "catalog_aliases": 8,
    "catalog_portions": 8,
    "supplements": 9,
    "supplement_logs": 9,
}

# Tables holding shared, non-personal reference data. Their totals are optional
# diagnostics — useful for spotting an unseeded catalog, but never a substitute
# for schema verification, and never counted as user data.
SHARED_TABLES: frozenset[str] = frozenset(
    {"catalog_foods", "catalog_aliases", "catalog_portions"}
)


def is_known_schema_version(version: int) -> bool:
    """Whether ``version`` is a schema version this checkout can certify.

    ``False`` for the legacy unversioned 0 and for any version newer than this
    checkout. Callers distinguish those two cases in their diagnostics, because
    they need different operator actions: migrate-with-rehearsal versus deploy a
    newer build.
    """
    return LEGACY_UNVERSIONED < int(version) <= LATEST_SCHEMA_VERSION


def required_tables_for(version: int) -> frozenset[str]:
    """The tables a database stamped at ``version`` must contain.

    Raises ``ValueError`` for a version this checkout cannot describe, so a
    caller can never silently verify against an empty required set — an unknown
    version must be an explicit decision, not a permissive default.
    """
    version = int(version)
    if not is_known_schema_version(version):
        raise ValueError(
            f"Cannot describe schema version {version}: this checkout knows "
            f"versions 1 through {LATEST_SCHEMA_VERSION}."
        )
    return frozenset(
        table
        for table, introduced in TABLE_INTRODUCED.items()
        if introduced <= version
    )


def user_tables_for(version: int) -> tuple[str, ...]:
    """User-owned tables required at ``version``, in a stable reporting order.

    Every user-owned table required at the stamped version is counted, so a
    diagnostic report cannot look complete while silently omitting the newest
    data. Only row *counts* are ever derived from these names.
    """
    required = required_tables_for(version)
    return tuple(
        table
        for table in sorted(required - SHARED_TABLES)
    )


def shared_tables_for(version: int) -> tuple[str, ...]:
    """Shared reference tables present at ``version``, in reporting order."""
    required = required_tables_for(version)
    return tuple(sorted(required & SHARED_TABLES))
