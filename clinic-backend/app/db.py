import logging

import asyncpg

from app.config import settings

logger = logging.getLogger("db")

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    """Create the asyncpg pool and verify the runtime role doesn't bypass
    row-level security. RLS is the demo's tenant fence - running as a
    superuser silently disables it and lets one tenant read another's data.
    """
    global _pool
    _pool = await asyncpg.create_pool(
        settings.database_url, min_size=1, max_size=10
    )

    # Closes review H1 - assert (with opt-out for dev) that the runtime
    # role isn't a superuser or BYPASSRLS role.
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT current_user::text AS role, rolsuper, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        )
    role = row["role"] if row else "<unknown>"
    is_super = bool(row and row["rolsuper"])
    is_bypass = bool(row and row["rolbypassrls"])

    if is_super or is_bypass:
        msg = (
            f"DB role {role!r} has rolsuper={is_super} rolbypassrls={is_bypass} "
            f"- row-level security is bypassed. Switch DATABASE_URL to a "
            f"NOSUPERUSER NOBYPASSRLS role (e.g. demo_app)."
        )
        if settings.allow_superuser_db_role:
            logger.critical("%s (allow_superuser_db_role=True; continuing)", msg)
        else:
            raise RuntimeError(msg)
    else:
        logger.info("DB role: %s (RLS enforced)", role)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized")
    return _pool
