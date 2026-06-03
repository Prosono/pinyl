import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, url_for
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")


def resolve_local_path(value, fallback: Path) -> Path:
    path = Path(value) if value else fallback
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


DATA_DIR = resolve_local_path(os.getenv("PINYL_DATA_DIR"), BASE_DIR / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
CARDS_FILE = DATA_DIR / "cards.json"
STATE_FILE = DATA_DIR / "state.json"
SPOTIFY_CACHE_FILE = resolve_local_path(os.getenv("SPOTIFY_CACHE_PATH"), DATA_DIR / ".spotify_cache")
SPOTIFY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_CARDS = {}
DEFAULT_STATE = {
    "last_seen_uid": None,
    "last_seen_at": None,
    "last_played_uid": None,
    "last_played_uri": None,
    "last_played_at": None,
    "last_error": None,
    "reader_name": None,
    "spotify_last_checked_at": None,
    "spotify_device_ready_at": None,
    "spotify_target_device_id": None,
    "spotify_last_error": None,
    "status": "idle",
}

SPOTIFY_SCOPES = "user-modify-playback-state user-read-playback-state user-read-currently-playing"
POLL_INTERVAL = float(os.getenv("NFC_POLL_INTERVAL", "0.4"))
DEBOUNCE_SECONDS = float(os.getenv("NFC_DEBOUNCE_SECONDS", "5"))
PORT = int(os.getenv("PORT", "8080"))
SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "change-me")
SPOTIFY_TRANSFER_DELAY = float(os.getenv("SPOTIFY_TRANSFER_DELAY", "0.8"))
SPOTIFY_WARMUP_ENABLED = env_bool("SPOTIFY_WARMUP_ENABLED", True)
SPOTIFY_WARMUP_START_DELAY = float(os.getenv("SPOTIFY_WARMUP_START_DELAY", "5"))
SPOTIFY_WARMUP_INTERVAL = float(os.getenv("SPOTIFY_WARMUP_INTERVAL", "300"))
SPOTIFY_WARMUP_RETRIES = int(os.getenv("SPOTIFY_WARMUP_RETRIES", "20"))
SPOTIFY_WARMUP_RESTART_AFTER = int(os.getenv("SPOTIFY_WARMUP_RESTART_AFTER", "4"))

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY

state_lock = threading.Lock()


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
        redirect_uri=get_spotify_redirect_uri(),
        scope=SPOTIFY_SCOPES,
        open_browser=False,
        cache_path=str(SPOTIFY_CACHE_FILE),
    )


def get_spotify_redirect_uri() -> str:
    return os.getenv("SPOTIFY_REDIRECT_URI", f"http://127.0.0.1:{PORT}/spotify/callback")


def spotify_ready() -> bool:
    return bool(
        os.getenv("SPOTIFY_CLIENT_ID")
        and os.getenv("SPOTIFY_CLIENT_SECRET")
    )


def preserve_refresh_token(oauth: SpotifyOAuth, token_info, refresh_token):
    if not token_info or not refresh_token or token_info.get("refresh_token"):
        return token_info

    token_info["refresh_token"] = refresh_token
    if oauth.cache_handler:
        oauth.cache_handler.save_token_to_cache(token_info)
    return token_info


def get_valid_token_info():
    oauth = spotify_oauth()

    cache_handler = oauth.cache_handler
    token_info = cache_handler.get_cached_token() if cache_handler else None

    if not token_info:
        raise RuntimeError("Spotify er ikke autorisert ennå.")

    refresh_token = token_info.get("refresh_token")

    if oauth.is_token_expired(token_info):
        if not refresh_token:
            raise RuntimeError("Spotify-token er utløpt og refresh token mangler. Koble til Spotify på nytt.")
        token_info = oauth.refresh_access_token(refresh_token)
        token_info = preserve_refresh_token(oauth, token_info, refresh_token)

    return oauth, token_info


def get_spotify_client() -> Spotify:
    oauth, token_info = get_valid_token_info()
    return Spotify(auth=token_info["access_token"], auth_manager=oauth)


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


def parse_spotify_reference(value: str):
    ref = normalize_spotify_reference(value)
    if not ref.startswith("spotify:"):
        return None, None

    parts = ref.split(":")
    if len(parts) < 3:
        return None, None

    item_type = parts[1]
    item_id = parts[2]
    return item_type, item_id


def get_spotify_metadata(ref: str):
    try:
        sp = get_spotify_client()
    except Exception:
        return None

    item_type, item_id = parse_spotify_reference(ref)
    if not item_type or not item_id:
        return None

    try:
        if item_type == "album":
            item = sp.album(item_id)
            images = item.get("images") or []
            artists = item.get("artists") or []
            return {
                "type": "Album",
                "title": item.get("name"),
                "subtitle": ", ".join(a.get("name", "") for a in artists if a.get("name")),
                "image": images[0]["url"] if images else None,
                "external_url": item.get("external_urls", {}).get("spotify"),
            }

        if item_type == "playlist":
            item = sp.playlist(item_id)
            images = item.get("images") or []
            owner = item.get("owner") or {}
            return {
                "type": "Spilleliste",
                "title": item.get("name"),
                "subtitle": owner.get("display_name") or "",
                "image": images[0]["url"] if images else None,
                "external_url": item.get("external_urls", {}).get("spotify"),
            }

        if item_type == "track":
            item = sp.track(item_id)
            album = item.get("album") or {}
            images = album.get("images") or []
            artists = item.get("artists") or []
            return {
                "type": "Låt",
                "title": item.get("name"),
                "subtitle": ", ".join(a.get("name", "") for a in artists if a.get("name")),
                "image": images[0]["url"] if images else None,
                "external_url": item.get("external_urls", {}).get("spotify"),
            }

        if item_type == "artist":
            item = sp.artist(item_id)
            images = item.get("images") or []
            return {
                "type": "Artist",
                "title": item.get("name"),
                "subtitle": "Artist",
                "image": images[0]["url"] if images else None,
                "external_url": item.get("external_urls", {}).get("spotify"),
            }

        if item_type == "episode":
            item = sp.episode(item_id)
            images = item.get("images") or []
            show = item.get("show") or {}
            return {
                "type": "Episode",
                "title": item.get("name"),
                "subtitle": show.get("name") or "",
                "image": images[0]["url"] if images else None,
                "external_url": item.get("external_urls", {}).get("spotify"),
            }

    except Exception:
        return None

    return None


def get_authorize_url() -> str:
    oauth = spotify_oauth()
    return oauth.get_authorize_url()


def list_devices(sp: Spotify):
    return sp.devices().get("devices", [])


def restart_raspotify():
    try:
        result = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", "raspotify"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return result.returncode == 0
    except Exception:
        return False


def find_matching_device(devices, target_name: str):
    normalized_target = target_name.strip().casefold()
    matches = [
        device
        for device in devices
        if (device.get("name") or "").strip().casefold() == normalized_target
    ]
    if not matches:
        return None
    return next((device for device in matches if device.get("is_active")), matches[0])


def find_target_device(sp: Spotify, retries: int = 30, restart_after: int = 8):
    target_name = get_device_name()
    restarted = False

    for attempt in range(retries):
        devices = list_devices(sp)
        target = find_matching_device(devices, target_name)
        if target:
            return target

        if attempt == restart_after and not restarted:
            update_state(
                status="waiting_for_spotify_device",
                last_error=f"Spotify-enheten '{target_name}' mangler. Restarter Raspotify...",
            )
            restarted = restart_raspotify()
            if not restarted:
                update_state(
                    spotify_last_error=(
                        "Kunne ikke restarte Raspotify automatisk. "
                        "Sjekk at pinyl-brukeren har sudo-rettighet til systemctl restart raspotify."
                    )
                )
            restarted = True
            time.sleep(10)
        else:
            time.sleep(1)

    raise RuntimeError(
        f"Fant ikke Spotify-enheten '{target_name}' etter {retries} sekunder. "
        "Raspotify/librespot kjører kanskje, men Spotify API returnerer ikke enheten ennå."
    )


def transfer_to_device(sp: Spotify, target, force_play: bool = False):
    device_id = target["id"]
    try:
        sp.transfer_playback(device_id=device_id, force_play=force_play)
        transferred = True
    except Exception as exc:
        update_state(spotify_last_error=f"Kunne ikke aktivere Spotify-enheten '{target.get('name')}': {exc}")
        transferred = False
    time.sleep(SPOTIFY_TRANSFER_DELAY)
    return transferred


def activate_target_device(
    sp: Spotify,
    force_play: bool = False,
    retries: int = 30,
    restart_after: int = 8,
):
    target = find_target_device(sp, retries=retries, restart_after=restart_after)
    transfer_to_device(sp, target, force_play=force_play)
    return target


def start_uri_on_device(sp: Spotify, device_id: str, uri: str):
    if uri.startswith(("spotify:album:", "spotify:playlist:", "spotify:artist:")):
        sp.start_playback(device_id=device_id, context_uri=uri)
    elif uri.startswith(("spotify:track:", "spotify:episode:")):
        sp.start_playback(device_id=device_id, uris=[uri])
    else:
        raise RuntimeError(f"Ukjent eller ugyldig Spotify-referanse: {uri}")


def play_uri(uri: str):
    sp = get_spotify_client()
    target = activate_target_device(sp)
    device_id = target["id"]

    uri = normalize_spotify_reference(uri)
    try:
        start_uri_on_device(sp, device_id, uri)
    except Exception:
        update_state(
            status="waiting_for_spotify_device",
            last_error="Spotify svarte ikke på første avspillingsforsøk. Prøver Raspotify på nytt...",
        )
        restart_raspotify()
        target = activate_target_device(sp, force_play=True)
        start_uri_on_device(sp, target["id"], uri)

    return target


def resume_playback():
    sp = get_spotify_client()
    target = activate_target_device(sp, force_play=True)
    sp.start_playback(device_id=target["id"])
    return target


def pause_playback():
    sp = get_spotify_client()
    target = find_target_device(sp)
    sp.pause_playback(device_id=target["id"])
    return target


def next_track():
    sp = get_spotify_client()
    target = activate_target_device(sp, force_play=True)
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


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def warmup_spotify():
    update_state(spotify_last_checked_at=now_iso())

    if not spotify_ready():
        update_state(
            spotify_last_error="Spotify API mangler client id eller client secret.",
            status="spotify_not_configured",
        )
        return False

    try:
        sp = get_spotify_client()
    except Exception as exc:
        update_state(
            spotify_last_error=str(exc),
            status="spotify_needs_login",
        )
        return False

    try:
        target = find_target_device(
            sp,
            retries=SPOTIFY_WARMUP_RETRIES,
            restart_after=SPOTIFY_WARMUP_RESTART_AFTER,
        )
        transferred = True
        playback = sp.current_playback()
        playback_device = (playback or {}).get("device") or {}
        if not ((playback or {}).get("is_playing") and playback_device.get("id") != target.get("id")):
            transferred = transfer_to_device(sp, target, force_play=False)

        updates = {
            "spotify_device_ready_at": now_iso(),
            "spotify_target_device_id": target.get("id"),
        }
        if transferred:
            updates["spotify_last_error"] = None
        update_state(**updates)
        return True
    except Exception as exc:
        update_state(
            spotify_last_error=str(exc),
            status="spotify_device_missing",
        )
        return False


def spotify_warmup_worker():
    time.sleep(SPOTIFY_WARMUP_START_DELAY)

    while True:
        warmup_spotify()
        time.sleep(SPOTIFY_WARMUP_INTERVAL)


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
                update_state(
                    last_seen_uid=uid,
                    last_seen_at=datetime.now().isoformat(timespec="seconds"),
                    status="card_seen",
                )

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
    try:
        spotify_authorized = bool(spotify_ready() and get_valid_token_info())
    except Exception:
        spotify_authorized = False

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
    cards = get_cards()
    enriched_cards = []

    for uid, card in cards.items():
        metadata = get_spotify_metadata(card.get("uri", ""))
        enriched_cards.append(
            {
                "uid": uid,
                "name": card.get("name") or uid,
                "uri": card.get("uri", ""),
                "notes": card.get("notes", ""),
                "meta": metadata,
            }
        )

    enriched_cards.sort(key=lambda c: (c["name"] or "").lower())

    return render_template(
        "cards.html",
        cards=enriched_cards,
        state=get_state(),
    )


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
    uri = None

    if request.is_json:
        payload = request.get_json(silent=True) or {}
        uri = payload.get("uri")
    else:
        uri = request.form.get("uri")

    try:
        if uri and uri.strip():
            target = play_uri(uri)
        else:
            target = resume_playback()

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
    return jsonify(
        {
            "state": get_state(),
            "playback": current_playback(),
            "devices": current_devices_safe(),
            "cards": get_cards(),
        }
    )


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
    previous_token_info = oauth.cache_handler.get_cached_token() if oauth.cache_handler else None
    previous_refresh_token = previous_token_info.get("refresh_token") if previous_token_info else None
    token_info = oauth.get_access_token(code=code, check_cache=False)
    preserve_refresh_token(oauth, token_info, previous_refresh_token)
    update_state(last_error=None, status="spotify_connected")
    return redirect(url_for("index"))


@app.get("/spotify/logout")
def spotify_logout():
    if SPOTIFY_CACHE_FILE.exists():
        SPOTIFY_CACHE_FILE.unlink()
    return redirect(url_for("index"))


if __name__ == "__main__":
    ensure_files()
    thread = threading.Thread(target=nfc_worker, daemon=True)
    thread.start()
    if SPOTIFY_WARMUP_ENABLED:
        spotify_thread = threading.Thread(target=spotify_warmup_worker, daemon=True)
        spotify_thread.start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
