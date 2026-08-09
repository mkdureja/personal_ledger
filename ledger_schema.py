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
* :data:`VERIFIED_COLUMNS` / :func:`required_columns_for` — the columns those
  tables must carry. A table can gain columns in a later migration, so each
  table maps to *groups* keyed by the version that introduced them; a database
  is only asked for a group its stamped version has reached.

A version number is an *input* to verification, not proof of correctness: a
corrupt, partially restored, or mis-stamped database must fail closed rather
than be adopted as a rollback point.
"""

from __future__ import annotations

# The version this checkout migrates a database to. Bump it (and register the new
# migration in ``bot.migrations._MIGRATIONS``) for every schema change.
LATEST_SCHEMA_VERSION = 15

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
    "exercises": 11,
    "gym_sets": 11,
    "meal_shortcuts": 12,
    "weight_logs": 13,
    "app_suggestions": 14,
}

# The columns each table must carry, grouped by the version that introduced
# them. Existence of a *table* is not proof of its shape: ``CREATE TABLE IF NOT
# EXISTS`` never adds a column to a table that already exists, and a column-only
# migration (v10) changes no table list at all — so a database can hold every
# required table, carry a current version stamp, and still be missing a column
# the runtime depends on.
#
# Grouping by version is what makes a column-only migration verifiable. A table
# appears once per version that changed its shape, and ``required_columns_for``
# unions every group at or below the version being checked, so a v9 rollback
# point is never asked for v10's columns.
#
# Listing a column that a valid database might legitimately lack would fail a
# *good* backup, so this map stays conservative: it names the columns the runtime
# reads, not every column in the DDL.
VERIFIED_COLUMNS: dict[str, tuple[tuple[int, tuple[str, ...]], ...]] = {
    "users": ((1, ("user_id", "username", "first_name")),),
    "study_logs": (
        (1, ("user_id", "subject", "duration_min", "notes", "logged_at")),
    ),
    "gym_logs": (
        (1, ("user_id", "exercise", "sets", "reps", "weight_kg", "logged_at")),
        # v11 rebuilds the table to make ``reps`` nullable (a varying-set
        # exercise has no single rep count) and stores the volume on the header
        # so a chart never has to read the per-set children.
        (11, ("total_volume_kg", "total_reps")),
    ),
    "diet_logs": (
        (
            1,
            (
                "user_id", "meal_type", "food_items", "calories",
                "protein_g", "carbs_g", "fat_g", "logged_at",
            ),
        ),
    ),
    "habits": ((1, ("user_id", "habit_name", "name_key", "is_active", "created_at")),),
    "habit_logs": ((1, ("user_id", "habit_id", "log_date")),),
    "user_settings": (
        (3, ("user_id", "reminders_enabled", "routine_profile")),
        # v10 is column-only: without this group a database that lost both
        # consent columns would still verify as a sound v10.
        (10, ("ai_parsing_enabled", "ai_parsing_consented_at")),
        # v15 rebuilds the table to make ai_parsing_enabled nullable, so the
        # columns are unchanged by name. Listed again at 15 so a database that
        # lost them cannot verify as a sound v15 either.
        (15, ("ai_parsing_enabled", "ai_parsing_consented_at")),
    ),
    "diet_log_items": (
        (
            8,
            (
                "id", "user_id", "diet_log_id", "item_order", "source_type",
                "source_id", "source_provider", "source_revision", "display_name",
                "calories", "protein_g", "carbs_g", "fat_g",
            ),
        ),
    ),
    "catalog_foods": (
        (8, ("id", "provider", "provider_food_id", "name_key", "base_unit", "is_active")),
    ),
    "catalog_portions": ((8, ("id", "catalog_food_id", "name_key", "base_amount")),),
    "catalog_aliases": ((8, ("id", "catalog_food_id", "alias_key")),),
    "supplements": (
        (
            9,
            (
                "id", "user_id", "name", "name_key", "dose_amount",
                "dose_unit", "timing", "is_active", "created_at",
            ),
        ),
    ),
    "supplement_logs": ((9, ("id", "user_id", "supplement_id", "log_date")),),
    "exercises": (
        (11, ("id", "user_id", "group_key", "name", "name_key", "is_active")),
    ),
    "gym_sets": (
        (11, ("id", "user_id", "gym_log_id", "set_number", "reps", "weight_kg")),
    ),
    "meal_shortcuts": (
        (12, ("id", "user_id", "meal_type", "source_type", "source_id")),
    ),
    "weight_logs": (
        (13, ("id", "user_id", "log_date", "weight_kg", "logged_at")),
    ),
    "app_suggestions": (
        (14, ("id", "user_id", "suggestion", "created_at")),
    ),
}


def required_columns_for(version: int) -> dict[str, frozenset[str]]:
    """The columns each table must carry in a database stamped at ``version``.

    Only tables whose shape is confirmed in :data:`VERIFIED_COLUMNS` appear;
    every other required table is existence-checked by the caller. Raises
    ``ValueError`` for an undescribable version, for the same reason
    :func:`required_tables_for` does.
    """
    version = int(version)
    if not is_known_schema_version(version):
        raise ValueError(
            f"Cannot describe schema version {version}: this checkout knows "
            f"versions 1 through {LATEST_SCHEMA_VERSION}."
        )
    required: dict[str, frozenset[str]] = {}
    for table, groups in VERIFIED_COLUMNS.items():
        if TABLE_INTRODUCED.get(table, LATEST_SCHEMA_VERSION + 1) > version:
            continue
        columns = {
            column
            for introduced, names in groups
            if introduced <= version
            for column in names
        }
        if columns:
            required[table] = frozenset(columns)
    return required


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
