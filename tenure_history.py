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
            PRIMARY KEY (guild_id, group_id)
        )
    """)


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
    """Only group join/leave events count; ignore rank-change events."""
    added = 0
    for event in events:
        if not isinstance(event, dict) or event.get("type") not in ("joined", "left"):
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

    Only closed WOM join->left event pairs and closed locally observed stints
    ending before the current in-game join are credited. Current stint itself
    is counted by the main sync engine.
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
        elif event_type == "left" and opened is not None:
            if opened < when <= current_joined_at:
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
    """Incremental, bounded group activity import. Older pages backfill over syncs.

    A failed/partial response never erases prior service. Pagination overlaps
    one page to accommodate new activity arriving while backfilling.
    """
    cursor.execute("""
        SELECT offset, complete, last_checked_at FROM tenure_backfill
        WHERE guild_id = ? AND group_id = ?
    """, (guild_id, group_id))
    row = cursor.fetchone()
    offset, complete, last_checked_at = row if row else (0, 0, None)
    last_checked = parse_timestamp(last_checked_at)
    now = datetime.datetime.now(UTC)
    if last_checked and (now - last_checked) < datetime.timedelta(hours=6):
        return 0

    # Always collect the newest events, then progressively older events.
    page_offsets = [0]
    if complete:
        page_offsets.extend([50, 100])  # cover busy periods between syncs
    elif offset:
        page_offsets.extend(range(max(0, offset - 50), offset + 950, 50))
    else:
        page_offsets.extend(range(50, 1000, 50))

    total = 0
    newest_backfill_offset = offset
    reached_end = bool(complete)
    fetched_successfully = False

    for page in dict.fromkeys(page_offsets):
        try:
            async with session.get(
                f"https://api.wiseoldman.net/v2/groups/{group_id}/activity",
                headers=headers, params={"limit": 50, "offset": page},
                timeout=30
            ) as response:
                if response.status != 200:
                    log.warning("WOM tenure history API: group %s returned %s", group_id, response.status)
                    break
                events = await response.json()
        except Exception as exc:
            log.warning("WOM tenure history unavailable for group %s: %s", group_id, exc)
            break

        if not isinstance(events, list):
            break
        fetched_successfully = True
        total += store_group_activity(cursor, guild_id, group_id, events)
        if not complete:
            newest_backfill_offset = max(newest_backfill_offset, page + len(events))
        if len(events) < 50:
            reached_end = True
            break

    cursor.execute("""
        INSERT INTO tenure_backfill
            (guild_id, group_id, offset, complete, last_checked_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, group_id) DO UPDATE SET
            offset = MAX(tenure_backfill.offset, excluded.offset),
            complete = MAX(tenure_backfill.complete, excluded.complete),
            last_checked_at = excluded.last_checked_at
    """, (
        guild_id, group_id, newest_backfill_offset, int(reached_end),
        iso(now) if fetched_successfully else last_checked_at
    ))
    return total


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
        elif kind == "left" and opened is not None:
            if joined_at and opened < moment <= joined_at:
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
