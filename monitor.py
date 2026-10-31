from datetime import datetime
from zoneinfo import ZoneInfo
import json
import os
import re
import sys
from pathlib import Path

import requests

BASE_URL = "https://www.reservix.de"
CONFIG_PATH = Path("events.json")
STATE_PATH = Path("state.json")
TIMEZONE = ZoneInfo("Europe/Berlin")

MAX_EVENTS_TO_MONITOR = 2

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


def telegram_send(message: str) -> None:
    """Send a Telegram notification."""
    if not TOKEN or not CHAT_ID:
        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
            "environment variables."
        )

    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": message},
        timeout=20,
    )
    response.raise_for_status()

    payload = response.json()

    if not payload.get("ok"):
        raise RuntimeError(
            f"Telegram API returned an error: {payload}"
        )


def read_json(path: Path, default):
    """Read a JSON file, returning a default if it does not exist."""
    if not path.exists():
        return default

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def get_event_datetime(event: dict) -> datetime:
    """Determine an event's local date and time."""
    event_date = datetime.strptime(
        event["date"], "%Y-%m-%d"
    ).date()

    title = event.get("title", "")

    match = re.search(
        r"\b(\d{1,2}):(\d{2})\s*Uhr\b",
        title,
    )

    hour, minute = (
        (int(match.group(1)), int(match.group(2)))
        if match
        else (0, 0)
    )

    return datetime(
        event_date.year,
        event_date.month,
        event_date.day,
        hour,
        minute,
        tzinfo=TIMEZONE,
    )


def get_upcoming_events(events):
    """Return the next configured upcoming events."""
    now = datetime.now(TIMEZONE)
    upcoming = []

    for event in events:
        if not isinstance(event, dict):
            continue

        if not event.get("date"):
            continue

        try:
            event_datetime = get_event_datetime(event)
        except (ValueError, TypeError, KeyError):
            continue

        if event_datetime >= now:
            upcoming.append((event_datetime, event))

    upcoming.sort(key=lambda item: item[0])

    return [
        event
        for _, event in upcoming[:MAX_EVENTS_TO_MONITOR]
    ]


def get_booked_count(event: dict) -> int:
    """
    Count unavailable seats according to Reservix.

    Unavailable does not necessarily mean confirmed purchases.
    """
    event_id = str(event["event_id"]).removeprefix("e")
    map_id = str(event["map_id"])

    event_url = event.get("event_url") or event.get("url")

    if not event_url:
        raise RuntimeError(
            f"Missing event URL for event {event_id}"
        )

    session = requests.Session()
    session.headers.update(HEADERS)

    page = session.get(event_url, timeout=25)
    page.raise_for_status()

    endpoint = (
        f"{BASE_URL}/ajax/seating-map/{map_id}/seat/{event_id}"
    )

    response = session.get(
        endpoint,
        headers={
            "Referer": event_url,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
        timeout=25,
    )

    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid seating-map JSON for event {event_id}"
        ) from exc

    seats = payload.get("data")

    if not isinstance(seats, list):
        raise RuntimeError(
            f"Unexpected seating-map response for event {event_id}"
        )

    return sum(
        1
        for seat in seats
        if isinstance(seat, dict)
        and seat.get("free") is False
    )


def save_state(state: dict) -> None:
    """Save state.json in a readable format."""
    with STATE_PATH.open("w", encoding="utf-8") as file:
        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=2,
        )
        file.write("\n")


def main():
    config = read_json(CONFIG_PATH, [])
    state = read_json(STATE_PATH, {})

    if not isinstance(config, list) or not config:
        raise RuntimeError(
            "events.json must contain a non-empty JSON list."
        )

    if not isinstance(state, dict):
        raise RuntimeError(
            "state.json must contain a JSON object."
        )

    events = get_upcoming_events(config)

    if not events:
        print("No upcoming events.")
        return

    print(
        f"Monitoring {len(events)} upcoming event(s)."
    )

    changed = False
    alerts = []

    for event in events:
        event_url = event.get("event_url") or event.get("url")

        required_fields = (
            "event_id",
            "map_id",
            "title",
            "date",
        )

        missing = [
            field
            for field in required_fields
            if not event.get(field)
        ]

        if not event_url:
            missing.append("event_url")

        if missing:
            print(
                f"Skipping invalid event: "
                f"missing {', '.join(missing)}"
            )
            continue

        event_id = str(
            event["event_id"]
        ).removeprefix("e")

        title = event["title"]
        state_key = f"{event_id}-{title}"

        print(f"\nChecking: {title}")

        # Retrieve the current count.
        try:
            current = get_booked_count(event)
        except Exception as exc:
            print(
                f"ERROR checking event {event_id}: {exc}",
                file=sys.stderr,
            )
            continue

        print(
            f"Unavailable seats: {current}"
        )

        # Migrate an existing numeric-only key.
        if state_key not in state and event_id in state:
            state[state_key] = state[event_id]
            del state[event_id]
            changed = True

        previous = state.get(state_key)

        if previous is None:
            # First observation: save baseline without notification.
            state[state_key] = current
            changed = True

            print(
                "Initial baseline saved. No notification sent."
            )

            continue

        try:
            previous = int(previous)
        except (ValueError, TypeError):
            print(
                f"Invalid previous count for {state_key}. "
                "Skipping update to avoid losing the baseline."
            )
            continue

        if current > previous:
            alerts.append(
                (state_key, current, previous, title)
            )

        if current != previous:
            state[state_key] = current
            changed = True

    # Send notifications before saving state.
    # Failed notifications retain the old count for retry.
    failed_notifications = []

    for state_key, current, previous, title in alerts:
        message = (
            f"{title}\n"
            f"Sold:{current}"
        )

        try:
            telegram_send(message)
            print(f"Telegram notification sent: {title}")
        except Exception as exc:
            print(
                f"ERROR sending Telegram notification: {exc}",
                file=sys.stderr,
            )

            # Restore the previous value so it can be retried.
            state[state_key] = previous
            failed_notifications.append(state_key)

    if changed:
        save_state(state)
        print("\nState saved to state.json.")

    print(
        f"\nFinished. "
        f"Alerts sent: {len(alerts) - len(failed_notifications)}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)