# Phase 1: Foundation

Implement versioned configuration, SQLAlchemy records, Alembic initialization,
task transitions and an engine-independent runner contract. Use PostgreSQL in
deployment and SQLite for focused tests. Initialize paused. Pin dependencies.

Acceptance: clean install, schema creation, CLI imports, graph validation and
invalid-state transition tests. Engine completion cannot itself mark work merged.
