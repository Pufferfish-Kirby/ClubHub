"""Club keys: how a club logs in to ClubHub (decision D17).

Each club holds its own random key. Presenting it as the `X-Club-Key` header proves "I am this
club", so that club's uploads may correct its own live events (routers/club.py), which a poster
from anyone else may not (D15). No accounts, no passwords, no student data: a key belongs to a
club, not a person.

Only SHA-256(key) is stored. A slow password hash (bcrypt) exists because people choose guessable
passwords; a 256-bit random key can't be guessed, so a fast hash is just as safe and lets the
lookup be one indexed query. A leaked database therefore leaks no usable keys.

James issues keys locally and commits the database, because Render's free tier forgets anything
written at runtime:

    python -m app.club_keys issue computer-science-students-union --label "CSSU exec 2026-27"
    python -m app.club_keys list
    python -m app.club_keys revoke computer-science-students-union
"""

import argparse
import hashlib
import secrets

from sqlmodel import Session, select

from app.models import Club, ClubKey
from app.timeutil import utc_now

PREFIX = "ck_"  # makes a pasted key recognisable, and greppable if one leaks into a log or commit


def new_key() -> str:
    return PREFIX + secrets.token_urlsafe(32)  # 32 random bytes = 256 bits


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def club_for_key(session: Session, key: str | None) -> Club | None:
    """The club this key logs in as, or None for a missing, unknown or revoked key."""
    if not key or not key.startswith(PREFIX):
        return None
    # Looking up by hash, not comparing strings, means there is no character-by-character
    # comparison whose timing could leak how much of a guess was right.
    row = session.exec(
        select(ClubKey).where(ClubKey.key_hash == hash_key(key), ClubKey.revoked_at.is_(None))
    ).first()
    return session.get(Club, row.club_id) if row else None


def issue_key(session: Session, club: Club, label: str | None = None) -> str:
    """Store a new key for `club` and return it. This is the only time the key exists in clear."""
    key = new_key()
    session.add(ClubKey(club_id=club.id, key_hash=hash_key(key), label=label))
    session.commit()
    return key


def _find_club(session: Session, name_or_slug: str) -> Club:
    wanted = name_or_slug.strip().casefold()
    for club in session.exec(select(Club)).all():
        slug = (club.sop_url or "").rstrip("/").rsplit("/", 1)[-1].casefold()
        if wanted in (club.name.casefold(), slug, club.id):
            return club
    raise SystemExit(f"No club named {name_or_slug!r}. Use its exact name, SOP slug or id.")


def _main() -> None:
    from app.database import engine, init_db

    parser = argparse.ArgumentParser(prog="python -m app.club_keys")
    sub = parser.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue", help="make a new key for a club (printed once)")
    issue.add_argument("club")
    issue.add_argument("--label", help="who it was given to")
    revoke = sub.add_parser("revoke", help="revoke every active key for a club")
    revoke.add_argument("club")
    sub.add_parser("list", help="show keys by club and label (never the keys themselves)")
    args = parser.parse_args()

    init_db()  # creates the clubkey table on a database that predates it
    with Session(engine) as session:
        if args.command == "issue":
            club = _find_club(session, args.club)
            key = issue_key(session, club, args.label)
            print(f"Key for {club.name} (shown once, send it to the club privately):\n{key}")
        elif args.command == "revoke":
            club = _find_club(session, args.club)
            rows = session.exec(
                select(ClubKey).where(ClubKey.club_id == club.id, ClubKey.revoked_at.is_(None))
            ).all()
            for row in rows:
                row.revoked_at = utc_now()
                session.add(row)
            session.commit()
            print(f"Revoked {len(rows)} key(s) for {club.name}.")
        else:
            for row in session.exec(select(ClubKey).order_by(ClubKey.created_at)).all():
                club = session.get(Club, row.club_id)
                state = f"revoked {row.revoked_at:%Y-%m-%d}" if row.revoked_at else "active"
                print(f"{club.name if club else row.club_id} | {row.label or '-'} | "
                      f"issued {row.created_at:%Y-%m-%d} | {state}")


if __name__ == "__main__":
    _main()
