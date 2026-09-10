"""Safely execute LLM-generated SQL.

Two layers of defense:
  1. sqlglot AST parse - reject anything that isn't a single SELECT.
  2. Postgres READ ONLY transaction + RLS scoped to session_id GUC.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

import sqlglot
import sqlglot.expressions as exp

from app.db import pool

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|grant|revoke|create|copy|merge|call|do)\b",
    re.IGNORECASE,
)

# Dangerous server-side functions, blocked at the parser regardless of casing
# or schema qualifier (`pg_catalog.set_config`):
#   - set_config/current_setting rewrite/read the transaction-local
#     `app.session_id` GUC mid-query -> RLS bypass (C1 in the review write-up).
#   - pg_sleep is a cheap per-request DoS: it pins a pool connection for the
#     full statement_timeout window with no rows to cap.
#   - the file / directory / large-object / network readers are superuser-gated
#     under today's NOSUPERUSER role, but become live file-read / SSRF the moment
#     the role is widened - block them here so the sandbox never depends on it.
_BANNED_FUNCTIONS = {
    "set_config",
    "current_setting",
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "dblink",
    "dblink_exec",
    "dblink_connect",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "query_to_xml",
    "database_to_xml",
}

# Postgres system schemas the LLM may never read. RLS does NOT cover system
# catalogs, so a SELECT against pg_stat_activity would leak other tenants'
# in-flight query text (their NL questions + generated SQL); information_schema /
# pg_settings leak schema + server config. Any table in these schemas - or any
# bare `pg_*` catalog relation (they resolve via the default search_path) - is
# rejected in _check_tables below.
_SYSTEM_SCHEMAS = {"pg_catalog", "information_schema", "pg_temp", "pg_toast"}


class UnsafeSQL(Exception):
    pass


def _has_banned_function(node) -> str | None:
    """Walk the AST; return the offending function name if present."""
    for fn in node.find_all(exp.Anonymous):
        # exp.Anonymous covers any unrecognized function call by name.
        name = (fn.name or "").lower()
        if name in _BANNED_FUNCTIONS:
            return name
    # sqlglot also has typed nodes for some functions; check by name on Func.
    for fn in node.find_all(exp.Func):
        name = (getattr(fn, "name", "") or "").lower()
        if name in _BANNED_FUNCTIONS:
            return name
    return None


def _check_tables(root, allowed_tables) -> None:
    """Reject system-catalog reads and, when an allowlist is supplied, any table
    outside it. CTE names defined in the same statement count as allowed so
    `WITH x AS (...) SELECT * FROM x` isn't falsely rejected."""
    cte_names = {(c.alias_or_name or "").lower() for c in root.find_all(exp.CTE)}
    allow = None
    if allowed_tables is not None:
        allow = {t.lower() for t in allowed_tables} | cte_names
    for tbl in root.find_all(exp.Table):
        schema = (tbl.db or "").lower()
        name = (tbl.name or "").lower()
        if schema in _SYSTEM_SCHEMAS or name.startswith("pg_"):
            raise UnsafeSQL(
                f"reference to system catalog is not allowed: {name or schema}"
            )
        if allow is not None and name and name not in allow:
            raise UnsafeSQL(f"table not in this demo's allowlist: {name}")


def validate(sql: str, *, allowed_tables: set[str] | list[str] | None = None) -> str:
    """Return cleaned SQL or raise UnsafeSQL. SELECT-only, single statement, no
    banned/dangerous functions, no system-catalog reads, and - when
    `allowed_tables` is given - only tables in that per-vertical allowlist."""
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise UnsafeSQL("empty SQL")

    if _FORBIDDEN_KEYWORDS.search(cleaned):
        raise UnsafeSQL("contains a forbidden keyword (write/DDL)")

    # Cheap pre-AST regex on the banned function names. Catches obvious
    # cases before sqlglot parsing; AST walk below is the authoritative check.
    if re.search(r"\b(set_config|current_setting)\s*\(", cleaned, re.IGNORECASE):
        raise UnsafeSQL("calls to set_config / current_setting are not allowed")

    try:
        statements = sqlglot.parse(cleaned, read="postgres")
    except Exception as e:
        raise UnsafeSQL(f"parse error: {e}") from e

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise UnsafeSQL("expected exactly one statement")

    root = statements[0]
    if not isinstance(root, (exp.Select, exp.Union, exp.With, exp.Subquery)):
        raise UnsafeSQL(f"not a SELECT (got {type(root).__name__})")

    banned = _has_banned_function(root)
    if banned:
        raise UnsafeSQL(f"forbidden function call: {banned}")

    _check_tables(root, allowed_tables)

    return cleaned


async def run_scoped(
    sql: str,
    *,
    session_id: UUID,
    row_cap: int = 200,
    allowed_tables: set[str] | list[str] | None = None,
) -> list[dict[str, Any]]:
    cleaned = validate(sql, allowed_tables=allowed_tables)

    async with pool().acquire() as conn:
        async with conn.transaction(readonly=True):
            # Bound a maliciously expensive query (CROSS JOIN, cartesian
            # explosion) so it can't tie up a pool connection. SET LOCAL
            # ties the timeout to this transaction only.
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            await conn.execute(
                "SELECT set_config('app.session_id', $1, true)",
                str(session_id),
            )
            rows = await conn.fetch(cleaned)

    out: list[dict[str, Any]] = []
    for r in rows[:row_cap]:
        out.append({k: _to_jsonable(v) for k, v in dict(r).items()})
    return out


def _to_jsonable(v: Any) -> Any:
    # asyncpg returns Decimal / datetime / UUID - make them JSON-safe.
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, UUID):
        return str(v)
    try:
        # Decimal
        from decimal import Decimal
        if isinstance(v, Decimal):
            return float(v)
    except Exception:
        pass
    return v
