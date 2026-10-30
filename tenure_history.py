"""Cumulative Novus clan tenure across separate membership periods.

Uses the current in-game clan join timestamp for the active stint, WOM group
join/leave activity for available historic stints, and local observations for
future departures. WOM group events are proxies, not exact in-game timestamps.
No inferred gaps are credited. Saved history is keyed by WOM player ID.
"""

import datetime
import logging
import sqlite3

log = logging.getLogger("WOMBot")
UTC = datetime.timezone.utc


def parse_timestamp(value):
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def iso(dt):
    return dt.astimezone(UTC).isoformat()


def initialize_tenure_tables(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tenure_observed (
            guild_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            player_id INTEGER NOT NULL,
            joined_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            closed_at TEXT,
            PRIMARY KEY (guild_id, group_id, player_id, joined_at)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tenure_activity (
            guild_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            player_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            event_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, group_id, player_id, event_type, event_at)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tenure_manual_credit (
            guild_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            player_id INTEGER NOT NULL,
            previous_days REAL NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, group_id, player_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tenure_backfill (
            guild_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            offset INTEGER NOT NULL DEFAULT 0,
            complete INTEGER NOT NULL DEFAULT 0,
            last_checked_at TEXT,
            activity_version INTEGER NOT NULL DEFAULT 2,
            PRIMARY KEY (guild_id, group_id)
        )
    """)

    # Older databases skipped rank-change activity. Re-scan historical
    # pages exactly once after upgrading, preserving all credited periods.
    cursor.execute("PRAGMA table_info(tenure_backfill)")
    columns = {row[1] for row in cursor.fetchall()}
    if "activity_version" not in columns:
        cursor.execute(
            "ALTER TABLE tenure_backfill ADD COLUMN "
            "activity_version INTEGER NOT NULL DEFAULT 1"
        )


def record_roster(cursor, guild_id, group_id, memberships, now=None):
    """Keep per-stint local observations; close when absent from a valid roster."""
    if not isinstance(memberships, list) or not memberships or not all(
        isinstance(m, dict)
        and isinstance(m.get("player"), dict)
        and isinstance(m["player"].get("id"), int)
        for m in memberships
    ):
        return 0

    now = now or datetime.datetime.now(UTC)
    current = {}
    present_ids = {m["player"]["id"] for m in memberships}
    for m in memberships:
        raw = m.get("clientSyncJoinedAt")
        if raw is None:
            raw = m["player"].get("clientSyncJoinedAt")
        joined = parse_timestamp(raw)
        if joined is None or joined > now:
            continue
        current[m["player"]["id"]] = joined

    cursor.execute("""
        SELECT player_id, joined_at, last_seen_at
        FROM tenure_observed
        WHERE guild_id = ? AND group_id = ? AND closed_at IS NULL
    """, (guild_id, group_id))
    for player_id, old_start, last_seen in cursor.fetchall():
        if player_id not in present_ids or (
            player_id in current and current[player_id] != parse_timestamp(old_start)
        ):
            # Conservative end: last verified day in WOM, never time outside it.
            cursor.execute("""
                UPDATE tenure_observed SET closed_at = ?
                WHERE guild_id = ? AND group_id = ?
                  AND player_id = ? AND joined_at = ? AND closed_at IS NULL
            """, (last_seen, guild_id, group_id, player_id, old_start))

    for player_id, joined in current.items():
        cursor.execute("""
            INSERT INTO tenure_observed
                (guild_id, group_id, player_id, joined_at, last_seen_at, closed_at)
            VALUES (?, ?, ?, ?, ?, NULL)
            ON CONFLICT(guild_id, group_id, player_id, joined_at)
            DO UPDATE SET last_seen_at = excluded.last_seen_at,
                          closed_at = NULL
        """, (guild_id, group_id, player_id, iso(joined), iso(now)))

    return len(current)


def store_group_activity(cursor, guild_id, group_id, events):
    """Record join/leave and rank changes, which prove group membership.

    Some initial group members may lack an explicit 'joined' activity
    because they were added when the WOM group was first created.
    """
    added = 0
    for event in events:
        if not isinstance(event, dict):
            continue

        # WOM explicitly identifies which group each event belongs to.
        # Never borrow membership activity from another clan, even if
        # that group also contains the same WOM player ID.
        if (
            event.get("groupId") != group_id
            or event.get("type") not in ("joined", "left", "changed_role")
        ):
            continue

        player_id = event.get("playerId")
        if player_id is None and isinstance(event.get("player"), dict):
            player_id = event["player"].get("id")
        event_at = parse_timestamp(event.get("createdAt"))
        if not isinstance(player_id, int) or event_at is None:
            continue
        cursor.execute("""
            INSERT OR IGNORE INTO tenure_activity
                (guild_id, group_id, player_id, event_type, event_at)
            VALUES (?, ?, ?, ?, ?)
        """, (guild_id, group_id, player_id, event["type"], iso(event_at)))
        added += cursor.rowcount
    return added


def prior_tenure(cursor, guild_id, group_id, player_id, current_joined_at):
    """Union confirmed older service intervals, avoiding double counting.

    Only closed WOM activity intervals and closed locally observed stints
    ending before the current in-game join are credited. A changed_role
    event proves the account was already in the WOM group and can anchor
    the start if a founding 'joined' event is missing. This is a lower
    bound, not an invented join date. Current stint is counted separately.
    """
    if player_id is None or current_joined_at is None:
        return datetime.timedelta(0)
    cursor.execute("""
        SELECT event_type, event_at FROM tenure_activity
        WHERE guild_id = ? AND group_id = ? AND player_id = ?
        ORDER BY event_at ASC
    """, (guild_id, group_id, player_id))
    intervals = []
    opened = None
    for event_type, stamp in cursor.fetchall():
        when = parse_timestamp(stamp)
        if when is None:
            continue
        if event_type == "joined":
            opened = when
        elif event_type == "changed_role" and opened is None:
            # An earlier rank change is evidence of membership, but not
            # proof that the member joined any earlier than that event.
            opened = when
        elif event_type == "left":
            if opened is not None and opened < when <= current_joined_at:
                intervals.append((opened, when))
            opened = None

    cursor.execute("""
        SELECT joined_at, closed_at FROM tenure_observed
        WHERE guild_id = ? AND group_id = ?
          AND player_id = ? AND closed_at IS NOT NULL
    """, (guild_id, group_id, player_id))
    for start, end in cursor.fetchall():
        start_dt, end_dt = parse_timestamp(start), parse_timestamp(end)
        if start_dt and end_dt and start_dt < end_dt <= current_joined_at:
            intervals.append((start_dt, end_dt))

    # Historical event windows and locally observed periods can overlap.
    # Merge their union before computing the duration.
    intervals.sort()
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return sum((end - start for start, end in merged), datetime.timedelta(0))


def get_manual_previous_tenure(cursor, guild_id, group_id, player_id):
    """Return a staff-verified previous-service floor (never additive to WOM)."""
    if group_id is None or player_id is None:
        return datetime.timedelta(0)
    cursor.execute("""
        SELECT previous_days FROM tenure_manual_credit
        WHERE guild_id = ? AND group_id = ? AND player_id = ?
    """, (guild_id, group_id, player_id))
    row = cursor.fetchone()
    if row is None:
        return datetime.timedelta(0)
    return datetime.timedelta(days=max(0, row[0]))


def set_manual_previous_tenure(cursor, guild_id, group_id, player_id, days):
    """Set verified previous clan service. Zero removes the manual credit.

    This is the TOTAL historical time spent in Novus before the current
    membership stint, not a value to add to other WOM-derived history.
    """
    if not isinstance(days, int) or not 0 <= days <= 3650:
        raise ValueError("Previous service must be an integer from 0 to 3650 days")
    if not isinstance(group_id, int) or not isinstance(player_id, int):
        raise ValueError("A linked WOM group and player ID are required")
    if days == 0:
        cursor.execute("""
            DELETE FROM tenure_manual_credit
            WHERE guild_id = ? AND group_id = ? AND player_id = ?
        """, (guild_id, group_id, player_id))
        return
    cursor.execute("""
        INSERT INTO tenure_manual_credit
            (guild_id, group_id, player_id, previous_days, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, group_id, player_id)
        DO UPDATE SET previous_days = excluded.previous_days,
                      updated_at = excluded.updated_at
    """, (guild_id, group_id, player_id, days, iso(datetime.datetime.now(UTC))))


def cumulative_effective_join(cursor, guild_id, group_id, player_id, joined_at):
    """Equivalent continuous start time from the largest verified previous-service record.

    A manually verified total is a floor, not extra service to double count
    on top of recorded WOM join/leave events.
    """
    if joined_at is None:
        return None, datetime.timedelta(0)
    recorded = prior_tenure(cursor, guild_id, group_id, player_id, joined_at)
    manual = get_manual_previous_tenure(cursor, guild_id, group_id, player_id)
    past = max(recorded, manual)
    return joined_at - past, past


async def refresh_activity_history(session, headers, cursor, guild_id, group_id):
    """Incrementally import WOM joined/left events without flooding its API.

    Group activity is ordered newest-first. Collect recent activity every
    refresh and resume historical pages across hourly refreshes. A one-page
    overlap helps tolerate offsets moving when new group events are added.
    The activity endpoint has a 50-item page limit and public rate limits,
    so each refresh fetches at most four pages. A completed historic backfill
    keeps checking the newest pages for future departures and rejoins.
    """
    cursor.execute("""
        SELECT offset, complete, last_checked_at FROM tenure_backfill
        WHERE guild_id = ? AND group_id = ?
    """, (guild_id, group_id))
    row = cursor.fetchone()
    offset, complete, last_checked_at = row if row else (0, 0, None)

    cursor.execute("""
        SELECT activity_version FROM tenure_backfill
        WHERE guild_id = ? AND group_id = ?
    """, (guild_id, group_id))
    existing = cursor.fetchone()
    if existing is not None and existing[0] < 2:
        # Revisit older pages now that rank-change evidence is supported.
        # Never delete previously imported membership activity or credit.
        cursor.execute("""
            UPDATE tenure_backfill SET offset = 0, complete = 0,
                last_checked_at = NULL, activity_version = 2
            WHERE guild_id = ? AND group_id = ?
        """, (guild_id, group_id))
        offset, complete, last_checked_at = 0, 0, None

    last_checked = parse_timestamp(last_checked_at)
    now = datetime.datetime.now(UTC)
    if last_checked and now - last_checked < datetime.timedelta(hours=1):
        return 0

    if complete:
        # Two pages cover up to 100 recent events between regular checks.
        pages = [0, 50]
    elif offset:
        # Current events, plus prior-page overlap and forward progress.
        pages = [0, max(0, offset - 50), offset, offset + 50]
    else:
        pages = [0, 50, 100, 150]

    added = 0
    highest_end = offset
    reached_end = bool(complete)
    fetched = False
    successful_pages = set()

    for page in dict.fromkeys(pages):
        try:
            async with session.get(
                f"https://api.wiseoldman.net/v2/groups/{group_id}/activity",
                headers=headers,
                params={"limit": 50, "offset": page},
                timeout=30,
            ) as response:
                if response.status != 200:
                    log.warning(
                        "WOM history group %s returned HTTP %s on offset %s",
                        group_id, response.status, page
                    )
                    break
                events = await response.json()
        except Exception as exc:
            log.warning(
                "WOM history group %s unavailable at offset %s: %s",
                group_id, page, exc
            )
            break

        if not isinstance(events, list):
            log.warning("WOM history group %s returned invalid activity", group_id)
            break

        fetched = True
        successful_pages.add(page)
        added += store_group_activity(
            cursor, guild_id, group_id, events
        )

        # Never advance the historical cursor based solely on a recent page
        # when resuming after an older offset.
        if not complete and page >= max(0, offset - 50):
            highest_end = max(highest_end, page + len(events))
            if len(events) < 50:
                reached_end = True
                break

    # Do not mark history complete on a short recent page if deeper history
    # was already being imported; an earlier transient gap is possible.
    cursor.execute("""
        INSERT INTO tenure_backfill
            (guild_id, group_id, offset, complete, last_checked_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, group_id) DO UPDATE SET
            offset = MAX(tenure_backfill.offset, excluded.offset),
            complete = MAX(tenure_backfill.complete, excluded.complete),
            last_checked_at = excluded.last_checked_at
    """, (
        guild_id, group_id, highest_end, int(reached_end),
        iso(now) if fetched else last_checked_at
    ))
    if fetched:
        log.info(
            "WOM group %s tenure history: %s new events, pages %s, "
            "historic offset %s, complete %s",
            group_id, added, sorted(successful_pages),
            highest_end, reached_end,
        )
    return added


def get_tenure_diagnostics(cursor, guild_id, group_id, player_id, joined_at):
    """Explain how much historical WOM tenure is documented and why.

    Does not infer in-game membership from group creation dates.
    """
    cursor.execute("""
        SELECT event_type, event_at FROM tenure_activity
        WHERE guild_id = ? AND group_id = ? AND player_id = ?
        ORDER BY event_at ASC
    """, (guild_id, group_id, player_id))
    events = cursor.fetchall()

    cursor.execute("""
        SELECT COUNT(*) FROM tenure_observed
        WHERE guild_id = ? AND group_id = ? AND player_id = ?
          AND closed_at IS NOT NULL
    """, (guild_id, group_id, player_id))
    observed_closed = cursor.fetchone()[0]

    cursor.execute("""
        SELECT offset, complete, last_checked_at
        FROM tenure_backfill WHERE guild_id = ? AND group_id = ?
    """, (guild_id, group_id))
    row = cursor.fetchone()
    offset, complete, last_checked = row if row else (0, 0, None)

    matched_previous_periods = 0
    opened = None
    for kind, timestamp in events:
        moment = parse_timestamp(timestamp)
        if moment is None:
            continue
        if kind == "joined":
            opened = moment
        elif kind == "changed_role" and opened is None:
            opened = moment
        elif kind == "left":
            if opened is not None and joined_at and opened < moment <= joined_at:
                matched_previous_periods += 1
            opened = None

    return {
        "events": events,
        "matched_previous_periods": matched_previous_periods,
        "closed_observed_stints": observed_closed,
        "history_offset": offset,
        "backfill_complete": bool(complete),
        "history_last_checked": last_checked,
        "wom_previous_days": prior_tenure(
            cursor, guild_id, group_id, player_id, joined_at
        ).total_seconds() / 86400 if joined_at else 0,
        "manual_previous_days": get_manual_previous_tenure(
            cursor, guild_id, group_id, player_id
        ).total_seconds() / 86400,
    }
