import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from flask import Flask, jsonify, redirect, render_template, request, url_for
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth
import re
import subprocess

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
CARDS_FILE = DATA_DIR / "cards.json"
STATE_FILE = DATA_DIR / "state.json"

DEFAULT_CARDS = {}
DEFAULT_STATE = {
    "last_seen_uid": None,
    "last_seen_at": None,
    "last_played_uid": None,
    "last_played_uri": None,
    "last_played_at": None,
    "last_error": None,
    "reader_name": None,
    "status": "idle",
}

SPOTIFY_SCOPES = "user-modify-playback-state user-read-playback-state user-read-currently-playing"
POLL_INTERVAL = float(os.getenv("NFC_POLL_INTERVAL", "0.4"))
DEBOUNCE_SECONDS = float(os.getenv("NFC_DEBOUNCE_SECONDS", "5"))
PORT = int(os.getenv("PORT", "8080"))
SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "change-me")

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY

state_lock = threading.Lock()
scan_waiters = set()


def ensure_files():
    if not CARDS_FILE.exists():
        CARDS_FILE.write_text(json.dumps(DEFAULT_CARDS, indent=2), encoding="utf-8")
    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps(DEFAULT_STATE, indent=2), encoding="utf-8")


def read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_cards():
    cards = read_json(CARDS_FILE, DEFAULT_CARDS.copy())
    if isinstance(cards, dict):
        normalized = {}
        for uid, value in cards.items():
            uid_norm = normalize_uid(uid)
            if isinstance(value, str):
                normalized[uid_norm] = {
                    "name": uid_norm,
                    "uri": normalize_spotify_reference(value),
                    "notes": "",
                }
            elif isinstance(value, dict):
                normalized[uid_norm] = {
                    "name": value.get("name") or uid_norm,
                    "uri": normalize_spotify_reference(value.get("uri", "")),
                    "notes": value.get("notes", ""),
                }
        return normalized
    return {}


def save_cards(cards):
    write_json(CARDS_FILE, cards)


def get_state():
    state = DEFAULT_STATE.copy()
    state.update(read_json(STATE_FILE, {}))
    return state


def update_state(**kwargs):
    with state_lock:
        state = get_state()
        state.update(kwargs)
        write_json(STATE_FILE, state)
        return state


def normalize_uid(uid: str) -> str:
    return "".join(ch for ch in uid.upper() if ch in "0123456789ABCDEF")


def spotify_oauth() -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=os.getenv("SPOTIFY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
        redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI", f"http://127.0.0.1:{PORT}/spotify/callback"),
        scope=SPOTIFY_SCOPES,
        open_browser=False,
        cache_path=str(DATA_DIR / ".spotify_cache"),
    )


def spotify_ready() -> bool:
    return bool(os.getenv("SPOTIFY_CLIENT_ID") and os.getenv("SPOTIFY_CLIENT_SECRET") and os.getenv("SPOTIFY_REDIRECT_URI"))


def get_spotify_client() -> Spotify:
    oauth = spotify_oauth()
    token_info = oauth.get_cached_token()
    if not token_info:
        raise RuntimeError("Spotify er ikke autorisert ennå.")
    return Spotify(auth_manager=oauth)


def get_device_name() -> str:
    return os.getenv("SPOTIFY_DEVICE_NAME", "Pinyl")


def normalize_spotify_reference(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return value
    if value.startswith("spotify:"):
        return value
    if "open.spotify.com" in value:
        parsed = urlparse(value)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2:
            item_type = parts[0]
            item_id = parts[1]
            return f"spotify:{item_type}:{item_id}"
    return value


def get_authorize_url() -> str:
    oauth = spotify_oauth()
    return oauth.get_authorize_url()


def list_devices(sp: Spotify):
    return sp.devices().get("devices", [])


def find_target_device(sp: Spotify):
    target_name = get_device_name()
    devices = list_devices(sp)
    for device in devices:
        if device.get("name") == target_name:
            return device
    raise RuntimeError(f"Fant ikke Spotify-enheten '{target_name}'. Åpne Spotify og bekreft at Raspotify/librespot kjører.")


def play_uri(uri: str):
    sp = get_spotify_client()
    target = find_target_device(sp)
    device_id = target["id"]
    sp.transfer_playback(device_id=device_id, force_play=True)
    time.sleep(0.8)

    uri = normalize_spotify_reference(uri)
    if uri.startswith(("spotify:album:", "spotify:playlist:", "spotify:artist:")):
        sp.start_playback(device_id=device_id, context_uri=uri)
    elif uri.startswith(("spotify:track:", "spotify:episode:")):
        sp.start_playback(device_id=device_id, uris=[uri])
    else:
        raise RuntimeError(f"Ukjent eller ugyldig Spotify-referanse: {uri}")
    return target


def pause_playback():
    sp = get_spotify_client()
    target = find_target_device(sp)
    sp.pause_playback(device_id=target["id"])
    return target


def next_track():
    sp = get_spotify_client()
    target = find_target_device(sp)
    sp.next_track(device_id=target["id"])
    return target


def current_playback():
    try:
        sp = get_spotify_client()
        return sp.current_playback()
    except Exception:
        return None


def current_devices_safe():
    try:
        sp = get_spotify_client()
        return list_devices(sp)
    except Exception:
        return []


def read_uid_once():
    try:
        result = subprocess.run(
            ["nfc-list", "-v"],
            capture_output=True,
            text=True,
            timeout=4,
        )

        output = (result.stdout or "") + "\n" + (result.stderr or "")

        if "ACS / ACR122U PICC Interface opened" in output:
            update_state(reader_name="ACS ACR122U")

        match = re.search(r"UID \(NFCID1\):\s*([0-9a-fA-F ]+)", output)
        if match:
            uid = "".join(match.group(1).split()).upper()
            return uid

        if "NFC device:" in output:
            return None

        update_state(reader_name=None, status="no_reader")
        return None

    except subprocess.TimeoutExpired:
        return None
    except Exception as exc:
        update_state(last_error=f"NFC-feil: {exc}", status="error")
        return None


def nfc_worker():
    last_uid = None
    last_seen_ts = 0.0
    while True:
        try:
            uid = read_uid_once()
            now = time.time()
            if uid:
                update_state(last_seen_uid=uid, last_seen_at=datetime.now().isoformat(timespec="seconds"), status="card_seen")
                if uid != last_uid or (now - last_seen_ts) > DEBOUNCE_SECONDS:
                    cards = get_cards()
                    if uid in cards and cards[uid].get("uri"):
                        try:
                            play_uri(cards[uid]["uri"])
                            update_state(
                                last_played_uid=uid,
                                last_played_uri=cards[uid]["uri"],
                                last_played_at=datetime.now().isoformat(timespec="seconds"),
                                last_error=None,
                                status="playing",
                            )
                        except Exception as exc:
                            update_state(last_error=f"Spotify-feil: {exc}", status="error")
                    else:
                        update_state(last_error=None, status="unknown_card")
                    last_uid = uid
                    last_seen_ts = now
                    scan_waiters.add(uid)
            else:
                if time.time() - last_seen_ts > 1.0:
                    last_uid = None
                current = get_state()
                if current.get("status") == "card_seen":
                    update_state(status="idle")
        except Exception as exc:
            update_state(last_error=f"Bakgrunnsfeil: {exc}", status="error")
        time.sleep(POLL_INTERVAL)


@app.template_filter("pretty_dt")
def pretty_dt(value):
    if not value:
        return "-"
    return value.replace("T", " ")


@app.route("/")
def index():
    state = get_state()
    cards = get_cards()
    playback = current_playback()
    devices = current_devices_safe()
    spotify_authorized = bool(spotify_ready() and spotify_oauth().get_cached_token()) if spotify_ready() else False
    return render_template(
        "index.html",
        state=state,
        cards=cards,
        playback=playback,
        devices=devices,
        spotify_ready=spotify_ready(),
        spotify_authorized=spotify_authorized,
        target_device=get_device_name(),
    )


@app.route("/cards")
def cards_page():
    return render_template("cards.html", cards=get_cards(), state=get_state())


@app.post("/cards/save")
def save_card_route():
    uid = normalize_uid(request.form.get("uid", ""))
    uri = normalize_spotify_reference(request.form.get("uri", ""))
    name = request.form.get("name", "").strip() or uid
    notes = request.form.get("notes", "").strip()
    if not uid or not uri:
        return redirect(url_for("cards_page"))

    cards = get_cards()
    cards[uid] = {"name": name, "uri": uri, "notes": notes}
    save_cards(cards)
    return redirect(url_for("cards_page"))


@app.post("/cards/delete/<uid>")
def delete_card(uid):
    uid = normalize_uid(uid)
    cards = get_cards()
    cards.pop(uid, None)
    save_cards(cards)
    return redirect(url_for("cards_page"))


@app.post("/play")
def play_route():
    uri = request.form.get("uri") or request.json.get("uri") if request.is_json else request.form.get("uri")
    try:
        target = play_uri(uri)
        update_state(last_error=None, status="playing")
        return jsonify({"ok": True, "device": target.get("name")})
    except Exception as exc:
        update_state(last_error=str(exc), status="error")
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/pause")
def pause_route():
    try:
        target = pause_playback()
        update_state(last_error=None, status="paused")
        return jsonify({"ok": True, "device": target.get("name")})
    except Exception as exc:
        update_state(last_error=str(exc), status="error")
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/next")
def next_route():
    try:
        target = next_track()
        update_state(last_error=None, status="playing")
        return jsonify({"ok": True, "device": target.get("name")})
    except Exception as exc:
        update_state(last_error=str(exc), status="error")
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/api/state")
def api_state():
    return jsonify({
        "state": get_state(),
        "playback": current_playback(),
        "devices": current_devices_safe(),
        "cards": get_cards(),
    })


@app.get("/api/wait-for-card")
def api_wait_for_card():
    timeout = time.time() + 20
    initial = get_state().get("last_seen_uid")
    while time.time() < timeout:
        state = get_state()
        uid = state.get("last_seen_uid")
        if uid and uid != initial:
            card = get_cards().get(uid)
            return jsonify({"ok": True, "uid": uid, "card": card, "state": state})
        time.sleep(0.25)
    return jsonify({"ok": False, "timeout": True, "state": get_state()})


@app.get("/spotify/login")
def spotify_login():
    if not spotify_ready():
        return redirect(url_for("index"))
    return redirect(get_authorize_url())


@app.get("/spotify/callback")
def spotify_callback():
    if not spotify_ready():
        return redirect(url_for("index"))
    code = request.args.get("code")
    if not code:
        return redirect(url_for("index"))
    oauth = spotify_oauth()
    oauth.get_access_token(code=code, check_cache=False)
    return redirect(url_for("index"))


@app.get("/spotify/logout")
def spotify_logout():
    cache = DATA_DIR / ".spotify_cache"
    if cache.exists():
        cache.unlink()
    return redirect(url_for("index"))


if __name__ == "__main__":
    ensure_files()
    thread = threading.Thread(target=nfc_worker, daemon=True)
    thread.start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
