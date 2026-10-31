"""RepoGuard SQLite storage layer.

Provides persistent local storage for scan results, configuration,
and related data using SQLite.
"""

from repoguard.store.database import RepoGuardDB

__all__ = ["RepoGuardDB"]
