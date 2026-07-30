"""Standalone maintenance entry points for the Ledger bot.

A package, not a loose directory, so ``python -m scripts.backup_db`` runs from
the repository root with the root itself on ``sys.path``. That is what lets a
standard-library-only script import the ``ledger_schema`` / ``ledger_backup``
contract without a path hack and without importing the application.
"""
