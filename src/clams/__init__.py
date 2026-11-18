"""Clams v2 — planner/executor/api over one SQLite DB and two Redis streams.

Invariants (enforced + tested):
  1. Single writer: only the planner mutates domain tables.
  2. DB is truth, queues are transport.
  3. Every side-effect via outbox.
  4. Evidence beats claims.
  5. Everything bounded.
"""
__version__ = "2.0.0"
