"""
Strava OAuth integration: authenticates locally, caches tokens, and prints
the last 30 activities with key stats.

Setup:
  1. In your Strava API app settings, add http://localhost:8080/callback
     as an Authorized Callback Domain.
  2. Copy .env.example to .env and fill in your Client ID and Client Secret.
  3. pip install -r requirements.txt
  4. python strava.py
"""

import json
import os
import time
import webbrowser
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


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def _build_auth_url(client_id: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "approval_prompt": "force",  # always show consent so scope is granted
        "scope": SCOPE,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _capture_auth_code() -> str:
    """Spin up a one-shot local server and wait for Strava's redirect."""
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
            pass  # suppress request logs

    server = HTTPServer(("localhost", 8080), Handler)
    server.handle_request()  # blocks until one request arrives

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


def _save_tokens(token_data: dict) -> None:
    with open(TOKEN_CACHE, "w") as f:
        json.dump({
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "expires_at": token_data["expires_at"],
        }, f)


def _load_tokens() -> dict | None:
    if not os.path.exists(TOKEN_CACHE):
        return None
    with open(TOKEN_CACHE) as f:
        return json.load(f)


def get_access_token(client_id: str, client_secret: str) -> str:
    """Return a valid access token, refreshing or re-authorizing as needed."""
    cached = _load_tokens()

    if cached:
        # Token still valid with a 60-second buffer
        if cached["expires_at"] > time.time() + 60:
            return cached["access_token"]

        # Token expired — use refresh token
        print("Access token expired, refreshing...")
        token_data = _refresh_token(client_id, client_secret, cached["refresh_token"])
        _save_tokens(token_data)
        return token_data["access_token"]

    # No cached token — full OAuth flow
    print("Opening browser for Strava authorization...")
    print("(If the browser doesn't open, visit this URL manually)")
    auth_url = _build_auth_url(client_id)
    print(f"\n  {auth_url}\n")
    webbrowser.open(auth_url)

    print("Waiting for authorization callback on http://localhost:8080/callback ...")
    code = _capture_auth_code()

    print("Exchanging authorization code for tokens...")
    token_data = _exchange_code(client_id, client_secret, code)
    _save_tokens(token_data)
    print(f"Tokens cached in {TOKEN_CACHE}\n")
    return token_data["access_token"]


# ---------------------------------------------------------------------------
# Activity fetching & formatting
# ---------------------------------------------------------------------------

def fetch_activities(access_token: str, count: int = 30) -> list[dict]:
    resp = requests.get(
        ACTIVITIES_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"per_page": count, "page": 1},
    )
    resp.raise_for_status()
    return resp.json()


def _meters_to_miles(m: float) -> float:
    return m / 1609.344


def _meters_to_feet(m: float) -> float:
    return m * 3.28084


def _seconds_to_hms(s: int) -> str:
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m {sec:02d}s" if h else f"{m}m {sec:02d}s"


def print_activities(activities: list[dict]) -> None:
    col = {"date": 12, "name": 32, "dist": 10, "elev": 10, "hr": 8, "time": 12}
    header = (
        f"{'DATE':<{col['date']}}"
        f"{'NAME':<{col['name']}}"
        f"{'DIST (mi)':<{col['dist']}}"
        f"{'ELEV (ft)':<{col['elev']}}"
        f"{'AVG HR':<{col['hr']}}"
        f"MOVING TIME"
    )
    divider = "─" * len(header)

    print(divider)
    print(header)
    print(divider)

    for act in activities:
        date = act["start_date_local"][:10]
        name = act["name"]
        if len(name) > col["name"] - 2:
            name = name[:col["name"] - 3] + "…"

        distance = _meters_to_miles(act.get("distance", 0))
        elevation = _meters_to_feet(act.get("total_elevation_gain", 0))
        avg_hr = act.get("average_heartrate")
        hr_str = f"{avg_hr:.0f} bpm" if avg_hr else "—"
        moving_time = _seconds_to_hms(act.get("moving_time", 0))

        print(
            f"{date:<{col['date']}}"
            f"{name:<{col['name']}}"
            f"{distance:<{col['dist']}.2f}"
            f"{elevation:<{col['elev']}.0f}"
            f"{hr_str:<{col['hr']}}"
            f"{moving_time}"
        )

    print(divider)
    print(f"  {len(activities)} activities\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    load_dotenv()
    client_id = os.getenv("STRAVA_CLIENT_ID", "").strip()
    client_secret = os.getenv("STRAVA_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        raise SystemExit(
            "Missing credentials. Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env"
        )

    access_token = get_access_token(client_id, client_secret)
    print("Fetching your last 30 activities...\n")
    activities = fetch_activities(access_token)

    if not activities:
        print("No activities found.")
        return

    print_activities(activities)


if __name__ == "__main__":
    main()
