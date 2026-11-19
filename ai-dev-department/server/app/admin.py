"""Admin CLI.

Usage:
    python -m app.admin create-invite [--expires-hours N]
    python -m app.admin list-invites
    python -m app.admin list-users
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Invite, User, utcnow


async def create_invite(expires_hours: int | None) -> int:
    await init_db()
    code = secrets.token_urlsafe(12)
    expires_at = None
    if expires_hours and expires_hours > 0:
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=expires_hours)).replace(tzinfo=None)

    async with SessionLocal() as session:
        invite = Invite(code=code, expires_at=expires_at)
        session.add(invite)
        await session.commit()

    print("Invite created.")
    print(f"  code:       {code}")
    if expires_at:
        print(f"  expires_at: {expires_at.isoformat()}Z")
    print("Share it with the user exactly once. It can be used a single time.")
    return 0


async def list_invites() -> int:
    await init_db()
    async with SessionLocal() as session:
        invites = (await session.scalars(select(Invite).order_by(Invite.id.desc()))).all()
    if not invites:
        print("No invites.")
        return 0
    for invite in invites:
        state = "used" if invite.used else ("expired" if invite.expires_at and invite.expires_at < utcnow() else "active")
        print(f"#{invite.id}  {state:8s}  created={invite.created_at.isoformat()}Z  code={invite.code}")
    return 0


async def list_users() -> int:
    await init_db()
    async with SessionLocal() as session:
        users = (await session.scalars(select(User).order_by(User.id))).all()
    if not users:
        print("No users.")
        return 0
    for user in users:
        role = "admin" if user.is_admin else "user"
        print(f"#{user.id}  {role:5s}  {user.email}  created={user.created_at.isoformat()}Z")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.admin", description="AI Dev Department admin CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create-invite", help="Create a single-use invite code")
    p_create.add_argument("--expires-hours", type=int, default=None, help="Optional invite expiry, in hours")

    sub.add_parser("list-invites", help="List invite codes and their state")
    sub.add_parser("list-users", help="List registered users")

    args = parser.parse_args()
    if args.command == "create-invite":
        return asyncio.run(create_invite(args.expires_hours))
    if args.command == "list-invites":
        return asyncio.run(list_invites())
    if args.command == "list-users":
        return asyncio.run(list_users())
    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
