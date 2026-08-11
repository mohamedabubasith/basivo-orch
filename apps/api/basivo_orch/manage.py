"""Administrative account commands.

Every account operation the app offers a user goes through their mailbox. That
is correct — and it means a deployment whose mail is not yet delivering has no
way to confirm an address or recover a password, which is exactly how a real
account ended up locked out of this one. This is the door that does not go
through email.

It is deliberately a local command rather than an HTTP endpoint. An endpoint
that can confirm any address and set any password is the single most valuable
target in the system; requiring shell access on the host means holding the
database already, so it grants nothing that was not already lost.

    uv run python -m basivo_orch.manage list
    uv run python -m basivo_orch.manage confirm you@example.com
    uv run python -m basivo_orch.manage set-password you@example.com
    uv run python -m basivo_orch.manage set-password you@example.com --password '…'

On the deployed host:

    docker compose -f deploy/docker-compose.prod.yml exec api \\
        python -m basivo_orch.manage confirm you@example.com
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import string
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from basivo_orch.auth.models import User
from basivo_orch.auth.security.passwords import hash_password
from basivo_orch.config import get_settings


def _sessionmaker():
    engine = create_async_engine(get_settings().DATABASE_URL)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _find(session: AsyncSession, email: str) -> User:
    # Addresses are stored lowercased by the login path; match that here rather
    # than reporting "no such user" for a capital letter.
    user = (
        await session.execute(select(User).where(User.email == email.strip().lower()))
    ).scalar_one_or_none()
    if user is None:
        sys.exit(f"No account with email {email!r}.")
    return user


def _generated_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "Basivo-" + "".join(secrets.choice(alphabet) for _ in range(16)) + "!"


async def cmd_list() -> None:
    engine, maker = _sessionmaker()
    async with maker() as session:
        users = (await session.execute(select(User).order_by(User.created_at))).scalars().all()
        if not users:
            print("No accounts.")
        for user in users:
            flags = []
            if not user.is_active:
                flags.append("inactive")
            if not user.is_verified:
                flags.append("unconfirmed")
            if user.is_superuser:
                flags.append("superuser")
            print(f"{user.email:40} {', '.join(flags) or 'ok'}")
    await engine.dispose()


async def cmd_confirm(email: str) -> None:
    engine, maker = _sessionmaker()
    async with maker() as session:
        user = await _find(session, email)
        if user.is_verified:
            print(f"{user.email} is already confirmed.")
        else:
            user.is_verified = True
            user.is_active = True
            await session.commit()
            print(f"Confirmed {user.email}.")
    await engine.dispose()


async def cmd_set_password(email: str, password: str | None) -> None:
    engine, maker = _sessionmaker()
    generated = password is None and not sys.stdin.isatty()
    if password is None:
        # A non-interactive shell cannot be prompted; generate and print one
        # rather than hanging on a getpass that never returns.
        password = _generated_password() if generated else getpass.getpass("New password: ")

    async with maker() as session:
        user = await _find(session, email)
        # The application's own hasher, so the running server verifies with the
        # same parameters it was written with.
        user.hashed_password = hash_password(password)
        user.is_active = True
        await session.commit()
        print(f"Password set for {user.email}.")
        if generated:
            print(f"Generated password: {password}")
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="basivo_orch.manage", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Every account and its state.")

    confirm = sub.add_parser("confirm", help="Mark an address confirmed without email.")
    confirm.add_argument("email")

    password = sub.add_parser("set-password", help="Set a password without email.")
    password.add_argument("email")
    password.add_argument("--password", help="Omit to be prompted, or to have one generated.")

    args = parser.parse_args()
    if args.command == "list":
        asyncio.run(cmd_list())
    elif args.command == "confirm":
        asyncio.run(cmd_confirm(args.email))
    elif args.command == "set-password":
        asyncio.run(cmd_set_password(args.email, args.password))


if __name__ == "__main__":
    main()
