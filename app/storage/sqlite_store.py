from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from threading import RLock
from typing import Iterator


class SQLiteStore:
    """One SQLite connection per service instance, serialized across threads."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:" and not self.db_path.startswith("file:"):
            Path(self.db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(
            self.db_path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
            uri=self.db_path.startswith("file:"),
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        self.migrate()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._connection.in_transaction:
                savepoint = "nested_" + uuid.uuid4().hex
                self._connection.execute(f"SAVEPOINT {savepoint}")
                try:
                    yield self._connection
                except BaseException:
                    self._connection.execute(f"ROLLBACK TO {savepoint}")
                    raise
                finally:
                    self._connection.execute(f"RELEASE {savepoint}")
                return
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._connection

    def migrate(self) -> None:
        with self.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
            version = row["version"] or 0
            if version > 9:
                raise RuntimeError(f"Database schema version {version} is newer than this service")
            if version == 0:
                schema = """
                    CREATE TABLE entries (
                        id TEXT PRIMARY KEY,
                        record_kind TEXT NOT NULL CHECK (record_kind IN ('meal', 'milk_tea')),
                        entry_date TEXT NOT NULL,
                        meal_type TEXT,
                        mode TEXT CHECK (mode IN ('eat_out', 'delivery', 'home_cooking') OR mode IS NULL),
                        meal_amount_cents INTEGER,
                        meal_amount_manual INTEGER NOT NULL DEFAULT 0 CHECK (meal_amount_manual IN (0, 1)),
                        location TEXT,
                        note TEXT,
                        timing_json TEXT NOT NULL DEFAULT '{}',
                        people_count INTEGER,
                        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        CHECK (
                            (record_kind = 'meal' AND meal_type IS NOT NULL AND mode IS NOT NULL)
                            OR (record_kind = 'milk_tea' AND meal_type IS NULL AND mode IS NULL)
                        )
                    );
                    CREATE INDEX entries_date_kind ON entries(record_kind, entry_date, created_at);

                    CREATE TABLE items (
                        item_id TEXT PRIMARY KEY,
                        entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        quantity TEXT,
                        unit TEXT,
                        notes TEXT
                    );
                    CREATE INDEX items_entry_id ON items(entry_id);

                    CREATE TABLE payments (
                        payment_id TEXT PRIMARY KEY,
                        entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL CHECK (kind IN ('meal', 'delivery_fee', 'packaging_fee', 'discount', 'milk_tea', 'purchase', 'other')),
                        scope TEXT NOT NULL CHECK (scope IN ('meal', 'extra')),
                        amount_cents INTEGER,
                        amount_min_cents INTEGER,
                        amount_max_cents INTEGER,
                        source TEXT NOT NULL CHECK (source IN ('actual', 'estimate', 'unknown')),
                        note TEXT,
                        CHECK (
                            (amount_cents IS NOT NULL AND amount_min_cents IS NULL AND amount_max_cents IS NULL)
                            OR (amount_cents IS NULL AND amount_min_cents IS NOT NULL AND amount_max_cents IS NOT NULL)
                            OR (amount_cents IS NULL AND amount_min_cents IS NULL AND amount_max_cents IS NULL)
                        ),
                        CHECK (amount_min_cents IS NULL OR amount_min_cents <= amount_max_cents)
                    );
                    CREATE INDEX payments_entry_id ON payments(entry_id);

                    CREATE TABLE candidates (
                        candidate_id TEXT PRIMARY KEY,
                        mode TEXT NOT NULL CHECK (mode IN ('eat_out', 'delivery', 'home_cooking')),
                        name TEXT NOT NULL,
                        details_json TEXT NOT NULL DEFAULT '{}',
                        preference TEXT NOT NULL DEFAULT 'neutral' CHECK (preference IN ('like', 'neutral', 'avoid')),
                        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX candidates_mode_name ON candidates(mode, name);

                    CREATE TABLE recipes (
                        recipe_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        details_json TEXT NOT NULL DEFAULT '{}',
                        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE evidence (
                        evidence_id TEXT PRIMARY KEY,
                        subject_type TEXT NOT NULL CHECK (subject_type IN ('candidate', 'recipe', 'location', 'meal')),
                        subject_id TEXT,
                        kind TEXT NOT NULL CHECK (kind IN ('time', 'cost')),
                        mode TEXT CHECK (mode IN ('eat_out', 'delivery', 'home_cooking') OR mode IS NULL),
                        location TEXT,
                        value_json TEXT NOT NULL,
                        source TEXT,
                        observed_at TEXT NOT NULL
                    );
                    CREATE INDEX evidence_subject ON evidence(subject_type, subject_id, kind, observed_at);

                    CREATE TABLE preferences (
                        preference_key TEXT PRIMARY KEY,
                        value_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE restrictions (
                        restriction_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL CHECK (kind IN ('allergy', 'temporary_avoidance')),
                        term TEXT NOT NULL,
                        active_from TEXT,
                        active_until TEXT,
                        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                        source TEXT,
                        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE request_receipts (
                        request_id TEXT PRIMARY KEY,
                        action TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    INSERT INTO schema_migrations(version, applied_at)
                    VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
                    """
                for statement in schema.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                version = 1
            if version == 1:
                connection.execute(
                    "ALTER TABLE entries ADD COLUMN consumption_cost_json TEXT NOT NULL "
                    "DEFAULT '{\"min_cents\":null,\"max_cents\":null,\"source\":\"unknown\"}'"
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                )
                version = 2
            if version == 2:
                connection.execute(
                    "CREATE TABLE stores ("
                    "store_id TEXT PRIMARY KEY, name TEXT NOT NULL, branch TEXT)"
                )
                connection.execute(
                    "CREATE UNIQUE INDEX stores_name_branch "
                    "ON stores(name, COALESCE(branch, ''))"
                )
                connection.execute(
                    "ALTER TABLE entries ADD COLUMN store_id TEXT REFERENCES stores(store_id)"
                )
                connection.execute("CREATE INDEX entries_store_id ON entries(store_id)")
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (3, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                )
                version = 3
            if version == 3:
                connection.create_function(
                    "canonical_calendar_date",
                    1,
                    lambda value: date.fromisoformat(value).isoformat(),
                    deterministic=True,
                )
                connection.execute("ALTER TABLE entries ADD COLUMN voided_at TEXT")
                connection.execute("ALTER TABLE entries ADD COLUMN void_reason TEXT")
                connection.execute("ALTER TABLE entries ADD COLUMN calendar_date TEXT")
                connection.execute(
                    "UPDATE entries SET calendar_date = canonical_calendar_date(entry_date)"
                )
                connection.execute(
                    "CREATE VIEW active_entries AS SELECT * FROM entries WHERE voided_at IS NULL"
                )
                from app.services.meal_history import MIGRATION_SQL

                for statement in MIGRATION_SQL:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (4, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                )
                version = 4
            if version == 4:
                from app.services.meal_analytics import MIGRATION_SQL as MEAL_ANALYTICS_MIGRATION_SQL
                from app.services.kitchen_recipe import MIGRATION_SQL as KITCHEN_RECIPE_MIGRATION_SQL
                from app.services.recommendation_history import MIGRATION_SQL as RECOMMENDATION_MIGRATION_SQL
                from app.services.kitchen import MIGRATION_SQL as KITCHEN_MIGRATION_SQL

                for migration_sql in (
                    MEAL_ANALYTICS_MIGRATION_SQL,
                    KITCHEN_RECIPE_MIGRATION_SQL,
                    RECOMMENDATION_MIGRATION_SQL,
                    KITCHEN_MIGRATION_SQL,
                ):
                    for statement in migration_sql:
                        connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (5, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                )
                version = 5

            if version == 5:
                from app.services.daybook import MIGRATION_SQL as DAYBOOK_MIGRATION_SQL
                from app.services.pricebook import MIGRATION_SQL as PRICEBOOK_MIGRATION_SQL
                from app.services.reminders import MIGRATION_SQL as REMINDER_MIGRATION_SQL

                for migration_sql in (DAYBOOK_MIGRATION_SQL, PRICEBOOK_MIGRATION_SQL, REMINDER_MIGRATION_SQL):
                    for statement in migration_sql:
                        connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (6, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                )
                version = 6

            if version == 6:
                from app.services.meal_options import MIGRATION_SQL as OPTION_MIGRATION_SQL
                from app.services.meal_corrections import MIGRATION_SQL as CORRECTION_MIGRATION_SQL
                from app.services.meal_planning import MIGRATION_SQL as PLANNING_MIGRATION_SQL
                from app.services.kitchen_operations import MIGRATION_SQL as KITCHEN_OPERATION_MIGRATION_SQL
                from app.services.meal_finance import MIGRATION_SQL as FINANCE_MIGRATION_SQL
                for statement in (
                    *OPTION_MIGRATION_SQL, *CORRECTION_MIGRATION_SQL, *PLANNING_MIGRATION_SQL,
                    *KITCHEN_OPERATION_MIGRATION_SQL, *FINANCE_MIGRATION_SQL,
                ):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (7, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                )
                version = 7
            if version == 7:
                connection.execute(
                    "CREATE INDEX entries_active_calendar "
                    "ON entries(record_kind, calendar_date DESC, created_at DESC, id DESC) "
                    "WHERE voided_at IS NULL"
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (8, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                )
                version = 8
            if version == 8:
                connection.execute(
                    "CREATE TABLE inventory_shelf_lives (ingredient_key TEXT NOT NULL, location_key TEXT NOT NULL, "
                    "days INTEGER NOT NULL CHECK(days >= 0), PRIMARY KEY(ingredient_key, location_key))"
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) "
                    "VALUES (9, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                )

    def close(self) -> None:
        with self._lock:
            self._connection.close()
