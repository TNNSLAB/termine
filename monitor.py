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

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
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
        raise RuntimeError(f"Telegram API returned an error: {payload}")


def read_json(path: Path, default):
    """Read a JSON file, returning a default if it does not exist."""
    if not path.exists():
        return default

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def get_event_datetime(event: dict) -> datetime:
    """
    Determine an event's local date and time.

    Reads the date from the 'date' field and, if available,
    the time from a title such as '18:00 Uhr'.
    Events without a recognizable time default to midnight.
    """
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


def get_next_event(events):
    """
    Return only the next upcoming event.

    Events are ordered by their date and, when available,
    the time in their title.
    """
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

    if not upcoming:
        return None

    return min(upcoming, key=lambda item: item[0])[1]


def get_booked_count(event: dict) -> int:
    """
    Retrieve the seating map and count seats where free is False.

    This represents unavailable seats according to the endpoint's
    response, which may not always mean confirmed purchases.
    """
    event_id = str(event["event_id"]).removeprefix("e")
    map_id = str(event["map_id"])
    event_url = event.get("event_url") or event.get("url")

    if not event_url:
        raise RuntimeError(
            f"Missing event URL for event {event_id}: "
            f"{event.get('title', 'Unknown event')}"
        )

    session = requests.Session()
    session.headers.update(HEADERS)

    # Visit the event page first to establish a session.
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
            f"Seating-map endpoint returned invalid JSON "
            f"for event {event_id}."
        ) from exc

    seats = payload.get("data")

    if not isinstance(seats, list):
        raise RuntimeError(
            f"Unexpected seating-map response for event {event_id}: "
            "expected a 'data' list."
        )

    return sum(
        1 for seat in seats
        if isinstance(seat, dict) and seat.get("free") is False
    )


def save_state(state: dict) -> None:
    """Write state.json in a readable format."""
    with STATE_PATH.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
        file.write("\n")


def main():
    config = read_json(CONFIG_PATH, [])
    state = read_json(STATE_PATH, {})

    if not isinstance(config, list) or not config:
        raise RuntimeError(
            "events.json must contain a non-empty JSON list of events."
        )

    if not isinstance(state, dict):
        raise RuntimeError(
            "state.json must contain a JSON object."
        )

    # Select only one event. The remaining events are not queried.
    event = get_next_event(config)

    if event is None:
        print("No upcoming events.")
        return

    # Support both existing URL field names.
    event_url = event.get("event_url") or event.get("url")
    if event_url:
        event["event_url"] = event_url

    for field in (
        "event_id",
        "map_id",
        "event_url",
        "title",
        "date",
    ):
        if not event.get(field):
            raise RuntimeError(
                f"Each event needs a non-empty '{field}': {event}"
            )

    event_id = str(event["event_id"]).removeprefix("e")
    title = event["title"]
    event_date = event["date"]

    # Human-readable key requested for state.json.
    state_key = f"{event_id}-{title}"

    print(f"Checking next event: {title}")
    print(f"Event date: {event_date}")

    # Query the seating map for this event only.
    current = get_booked_count(event)

    print(
        f"{title} (event {event_id}): "
        f"{current} unavailable/booked seats"
    )

    # Migrate an old numeric-only key without losing its baseline.
    changed = False

    if state_key not in state and event_id in state:
        state[state_key] = state.pop(event_id)
        changed = True

    previous = state.get(state_key)
    alerts = []

    if previous is None:
        # First observation: save a baseline without sending an alert.
        state[state_key] = current
        changed = True

        print(
            f"Initial baseline saved for event {event_id}; "
            "no alert sent."
        )

    else:
        previous = int(previous)

        if current > previous:
            increase = current - previous

            alerts.append(
                f"{title}\n"
                f"Sold:"
                f"{current}"
            )

        if current != previous:
            state[state_key] = current
            changed = True

    # Send notifications before saving state.
    # If sending fails, the old seat count remains available for retry.
    for alert in alerts:
        telegram_send(alert)
        print("Telegram notification sent.")

    if changed:
        save_state(state)
        print("State saved to state.json.")

    print(f"Finished. Alerts sent: {len(alerts)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)