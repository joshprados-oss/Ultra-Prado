"""
Strava OAuth integration — activity stats, weekly summaries, and CSV export.

Usage:
  python strava.py                       # last 30 activities (all types)
  python strava.py --count 60            # last 60 activities
  python strava.py --type Run            # filter by sport type (case-insensitive)
  python strava.py --since 2026-01-01    # activities on or after a date
  python strava.py --weeks               # aggregate into weekly summaries
  python strava.py --export output.csv   # also write results to CSV
  python strava.py --type Ride --weeks   # combine flags freely

Setup:
  1. In your Strava API app settings, add localhost as an Authorized Callback Domain.
  2. Copy .env.example to .env and fill in your Client ID and Client Secret.
  3. pip install -r requirements.txt
  4. python strava.py
"""

import argparse
import csv
import json
import os
import time
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from dotenv import load_dotenv

AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
REDIRECT_URI = "http://localhost:8080/callback"
SCOPE = "activity:read_all"
TOKEN_CACHE = "strava_tokens.json"

# Sport types where pace (min/mi) is more natural than speed (mph)
_PACE_TYPES = {"run", "trailrun", "virtualrun", "walk", "hike"}
# Sport types where speed (mph) is more natural
_SPEED_TYPES = {"ride", "mountainbikeride", "gravelride", "virtualride", "ebikeride"}


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def _build_auth_url(client_id: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "approval_prompt": "force",
        "scope": SCOPE,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _capture_auth_code() -> str:
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            params = parse_qs(urlparse(self.path).query)
            if "code" in params:
                captured["code"] = params["code"][0]
                body = b"<h2>Authorization successful! You can close this tab.</h2>"
                self.send_response(200)
            else:
                error = params.get("error", ["unknown"])[0]
                body = f"<h2>Authorization failed: {error}</h2>".encode()
                self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = HTTPServer(("localhost", 8080), Handler)
    server.handle_request()
    if "code" not in captured:
        raise RuntimeError("Authorization failed — no code returned by Strava.")
    return captured["code"]


def _exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    resp = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
    })
    resp.raise_for_status()
    return resp.json()


def _refresh_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    resp = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    resp.raise_for_status()
    return resp.json()


def _save_tokens(data: dict) -> None:
    with open(TOKEN_CACHE, "w") as f:
        json.dump({k: data[k] for k in ("access_token", "refresh_token", "expires_at")}, f)


def _load_tokens() -> dict | None:
    if not os.path.exists(TOKEN_CACHE):
        return None
    with open(TOKEN_CACHE) as f:
        return json.load(f)


def get_access_token(client_id: str, client_secret: str) -> str:
    cached = _load_tokens()

    if cached:
        if cached["expires_at"] > time.time() + 60:
            return cached["access_token"]
        print("Access token expired, refreshing...")
        data = _refresh_token(client_id, client_secret, cached["refresh_token"])
        _save_tokens(data)
        return data["access_token"]

    print("Opening browser for Strava authorization...")
    auth_url = _build_auth_url(client_id)
    print(f"\n  {auth_url}\n")
    webbrowser.open(auth_url)
    print("Waiting for callback on http://localhost:8080/callback ...")
    code = _capture_auth_code()
    print("Exchanging code for tokens...")
    data = _exchange_code(client_id, client_secret, code)
    _save_tokens(data)
    print(f"Tokens cached in {TOKEN_CACHE}\n")
    return data["access_token"]


# ---------------------------------------------------------------------------
# Activity fetching
# ---------------------------------------------------------------------------

def fetch_activities(
    access_token: str,
    count: int = 30,
    after: int | None = None,
    sport_type: str | None = None,
) -> list[dict]:
    """Fetch up to `count` activities, paginating as needed."""
    headers = {"Authorization": f"Bearer {access_token}"}
    results: list[dict] = []
    page = 1

    while len(results) < count:
        batch_size = min(count - len(results), 200)
        params: dict = {"per_page": batch_size, "page": page}
        if after is not None:
            params["after"] = after

        resp = requests.get(ACTIVITIES_URL, headers=headers, params=params)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break

        if sport_type:
            needle = sport_type.lower()
            batch = [a for a in batch if a.get("sport_type", "").lower() == needle]

        results.extend(batch)
        if len(batch) < batch_size:
            break
        page += 1

    return results[:count]


# ---------------------------------------------------------------------------
# Unit conversions & formatting
# ---------------------------------------------------------------------------

def _m_to_mi(m: float) -> float:
    return m / 1609.344


def _m_to_ft(m: float) -> float:
    return m * 3.28084


def _sec_to_hms(s: int) -> str:
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m {sec:02d}s" if h else f"{m}m {sec:02d}s"


def _pace_str(moving_time_s: int, distance_m: float) -> str:
    """min:sec per mile, e.g. '8:32 /mi'"""
    if distance_m < 10:
        return "—"
    secs_per_mile = moving_time_s / _m_to_mi(distance_m)
    m, s = divmod(int(secs_per_mile), 60)
    return f"{m}:{s:02d} /mi"


def _speed_str(moving_time_s: int, distance_m: float) -> str:
    """mph, e.g. '18.4 mph'"""
    if moving_time_s < 1 or distance_m < 10:
        return "—"
    mph = _m_to_mi(distance_m) / (moving_time_s / 3600)
    return f"{mph:.1f} mph"


def _pace_or_speed(sport_type: str, moving_time_s: int, distance_m: float) -> str:
    key = sport_type.lower()
    if key in _PACE_TYPES:
        return _pace_str(moving_time_s, distance_m)
    if key in _SPEED_TYPES:
        return _speed_str(moving_time_s, distance_m)
    return "—"


def _short_type(sport_type: str) -> str:
    mapping = {
        "run": "Run", "trailrun": "Trail", "virtualrun": "VRun",
        "ride": "Ride", "mountainbikeride": "MTB", "gravelride": "Gravel",
        "virtualride": "VRide", "ebikeride": "eBike",
        "swim": "Swim", "walk": "Walk", "hike": "Hike",
        "workout": "Wrkout", "weighttraining": "Wghts",
        "yoga": "Yoga", "crossfit": "CF",
    }
    return mapping.get(sport_type.lower(), sport_type[:6])


def _week_monday(date_str: str) -> str:
    d = datetime.fromisoformat(date_str[:10])
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Display: individual activities
# ---------------------------------------------------------------------------

C = {"date": 12, "type": 7, "name": 28, "dist": 10, "elev": 10, "pace": 11, "hr": 10, "time": 12}


def _activity_row(act: dict) -> str:
    date = act["start_date_local"][:10]
    sport = act.get("sport_type", act.get("type", ""))
    stype = _short_type(sport)
    name = act["name"]
    if len(name) > C["name"] - 1:
        name = name[:C["name"] - 2] + "…"
    dist_m = act.get("distance", 0)
    dist_mi = _m_to_mi(dist_m)
    elev_ft = _m_to_ft(act.get("total_elevation_gain", 0))
    mv = act.get("moving_time", 0)
    pace = _pace_or_speed(sport, mv, dist_m)
    avg_hr = act.get("average_heartrate")
    hr_str = f"{avg_hr:.0f} bpm" if avg_hr else "—"

    return (
        f"{date:<{C['date']}}"
        f"{stype:<{C['type']}}"
        f"{name:<{C['name']}}"
        f"{dist_mi:<{C['dist']}.2f}"
        f"{elev_ft:<{C['elev']}.0f}"
        f"{pace:<{C['pace']}}"
        f"{hr_str:<{C['hr']}}"
        f"{_sec_to_hms(mv)}"
    )


def _activity_header() -> str:
    return (
        f"{'DATE':<{C['date']}}"
        f"{'TYPE':<{C['type']}}"
        f"{'NAME':<{C['name']}}"
        f"{'DIST (mi)':<{C['dist']}}"
        f"{'ELEV (ft)':<{C['elev']}}"
        f"{'PACE / SPD':<{C['pace']}}"
        f"{'AVG HR':<{C['hr']}}"
        f"MOVING TIME"
    )


def _totals_row(activities: list[dict]) -> str:
    total_dist = sum(_m_to_mi(a.get("distance", 0)) for a in activities)
    total_elev = sum(_m_to_ft(a.get("total_elevation_gain", 0)) for a in activities)
    total_time = sum(a.get("moving_time", 0) for a in activities)
    hrs = [a["average_heartrate"] for a in activities if a.get("average_heartrate")]
    avg_hr_str = f"{sum(hrs)/len(hrs):.0f} bpm" if hrs else "—"

    label = f"TOTAL ({len(activities)} activities)"
    return (
        f"{'':>{C['date']}}"
        f"{'':>{C['type']}}"
        f"{label:<{C['name']}}"
        f"{total_dist:<{C['dist']}.2f}"
        f"{total_elev:<{C['elev']}.0f}"
        f"{'':>{C['pace']}}"
        f"{avg_hr_str:<{C['hr']}}"
        f"{_sec_to_hms(total_time)}"
    )


def print_activities(activities: list[dict]) -> None:
    header = _activity_header()
    div = "─" * len(header)
    print(div)
    print(header)
    print(div)
    for act in activities:
        print(_activity_row(act))
    print(div)
    print(_totals_row(activities))
    print(div)
    print()


# ---------------------------------------------------------------------------
# Display: weekly summaries
# ---------------------------------------------------------------------------

WC = {"week": 12, "acts": 7, "types": 24, "dist": 10, "elev": 10, "time": 14, "hr": 10}


def print_weekly(activities: list[dict]) -> None:
    weeks: dict[str, list[dict]] = defaultdict(list)
    for act in activities:
        weeks[_week_monday(act["start_date_local"])].append(act)

    header = (
        f"{'WEEK (Mon)':<{WC['week']}}"
        f"{'ACTS':<{WC['acts']}}"
        f"{'TYPES':<{WC['types']}}"
        f"{'DIST (mi)':<{WC['dist']}}"
        f"{'ELEV (ft)':<{WC['elev']}}"
        f"{'MOVING TIME':<{WC['time']}}"
        f"AVG HR"
    )
    div = "─" * len(header)
    print(div)
    print(header)
    print(div)

    grand_dist = grand_elev = grand_time = 0.0
    grand_hrs: list[float] = []

    for week in sorted(weeks.keys(), reverse=True):
        acts = weeks[week]
        dist = sum(_m_to_mi(a.get("distance", 0)) for a in acts)
        elev = sum(_m_to_ft(a.get("total_elevation_gain", 0)) for a in acts)
        mv = sum(a.get("moving_time", 0) for a in acts)
        hrs = [a["average_heartrate"] for a in acts if a.get("average_heartrate")]
        hr_str = f"{sum(hrs)/len(hrs):.0f} bpm" if hrs else "—"

        type_counts: dict[str, int] = defaultdict(int)
        for a in acts:
            type_counts[_short_type(a.get("sport_type", a.get("type", "?")))] += 1
        types_str = " ".join(f"{t}×{n}" for t, n in sorted(type_counts.items()))
        if len(types_str) > WC["types"] - 1:
            types_str = types_str[:WC["types"] - 2] + "…"

        print(
            f"{week:<{WC['week']}}"
            f"{len(acts):<{WC['acts']}}"
            f"{types_str:<{WC['types']}}"
            f"{dist:<{WC['dist']}.2f}"
            f"{elev:<{WC['elev']}.0f}"
            f"{_sec_to_hms(mv):<{WC['time']}}"
            f"{hr_str}"
        )

        grand_dist += dist
        grand_elev += elev
        grand_time += mv
        grand_hrs.extend(hrs)

    grand_hr_str = f"{sum(grand_hrs)/len(grand_hrs):.0f} bpm" if grand_hrs else "—"
    print(div)
    print(
        f"{'TOTAL':<{WC['week']}}"
        f"{len(activities):<{WC['acts']}}"
        f"{'':>{WC['types']}}"
        f"{grand_dist:<{WC['dist']}.2f}"
        f"{grand_elev:<{WC['elev']}.0f}"
        f"{_sec_to_hms(int(grand_time)):<{WC['time']}}"
        f"{grand_hr_str}"
    )
    print(div)
    print()


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def export_csv(activities: list[dict], path: str) -> None:
    fields = [
        "date", "sport_type", "name",
        "distance_mi", "elevation_ft", "pace_or_speed",
        "average_heartrate", "max_heartrate",
        "moving_time_s", "moving_time",
        "suffer_score", "kudos_count", "id",
    ]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for act in activities:
            sport = act.get("sport_type", act.get("type", ""))
            dist_m = act.get("distance", 0)
            mv = act.get("moving_time", 0)
            w.writerow({
                "date": act["start_date_local"][:10],
                "sport_type": sport,
                "name": act["name"],
                "distance_mi": f"{_m_to_mi(dist_m):.4f}",
                "elevation_ft": f"{_m_to_ft(act.get('total_elevation_gain', 0)):.1f}",
                "pace_or_speed": _pace_or_speed(sport, mv, dist_m),
                "average_heartrate": act.get("average_heartrate", ""),
                "max_heartrate": act.get("max_heartrate", ""),
                "moving_time_s": mv,
                "moving_time": _sec_to_hms(mv),
                "suffer_score": act.get("suffer_score", ""),
                "kudos_count": act.get("kudos_count", 0),
                "id": act["id"],
            })
    print(f"Exported {len(activities)} activities to {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fetch and display your Strava activities.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python strava.py\n"
            "  python strava.py --count 60 --type Run\n"
            "  python strava.py --since 2026-01-01 --weeks\n"
            "  python strava.py --export activities.csv\n"
        ),
    )
    p.add_argument("--count", type=int, default=30, metavar="N",
                   help="Number of activities to fetch (default: 30)")
    p.add_argument("--type", dest="sport_type", metavar="TYPE",
                   help="Filter by sport type, e.g. Run, Ride, Swim (case-insensitive)")
    p.add_argument("--since", metavar="YYYY-MM-DD",
                   help="Only include activities on or after this date")
    p.add_argument("--weeks", action="store_true",
                   help="Show weekly aggregates instead of individual activities")
    p.add_argument("--export", metavar="FILE",
                   help="Write results to a CSV file")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    load_dotenv()
    client_id = os.getenv("STRAVA_CLIENT_ID", "").strip()
    client_secret = os.getenv("STRAVA_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise SystemExit(
            "Missing credentials. Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env"
        )

    access_token = get_access_token(client_id, client_secret)

    after: int | None = None
    if args.since:
        after = int(datetime.fromisoformat(args.since).timestamp())

    type_label = f" ({args.sport_type})" if args.sport_type else ""
    print(f"Fetching up to {args.count} activities{type_label}...\n")

    activities = fetch_activities(
        access_token,
        count=args.count,
        after=after,
        sport_type=args.sport_type,
    )

    if not activities:
        print("No activities found.")
        return

    if args.weeks:
        print_weekly(activities)
    else:
        print_activities(activities)

    if args.export:
        export_csv(activities, args.export)


if __name__ == "__main__":
    main()
