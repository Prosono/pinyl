# Pinyl webapp

Lokal webapp for Raspberry Pi som:

- overvåker en ACR122U NFC-leser
- mapper NFC UID-er til Spotify-album, spillelister eller spor
- spiller på en lokal Spotify Connect-enhet som Raspotify/librespot
- gir et enkelt admin-grensesnitt for å registrere og administrere kort

## Funksjoner

- Dashboard med status for leser, siste skann og siste avspilling
- Spotify-innlogging via Spotify OAuth
- Test av Spotify-URI eller vanlig Spotify-lenke
- Kortadministrasjon i nettleser
- "Vent på neste tag" for enkel registrering av nye NFC-kort
- JSON-lagring lokalt på Pi-en

## Forutsetninger

- Raspberry Pi med Python 3
- ACR122U
- `pcscd` installert og kjørende
- Raspotify/librespot satt opp og synlig som Spotify Connect-enhet
- Spotify Premium

## Installer systempakker

```bash
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip python3-dev \
  libpcsclite-dev pcscd pcsc-tools libacsccid1 \
  swig pkg-config
```

## Oppsett

```bash
git clone <your-repo-url>
cd pinyl_webapp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
mkdir -p data
```

Rediger `.env` og fyll inn Spotify-verdiene.

## Spotify-app

Opprett en app i Spotify Developer Dashboard og legg inn redirect URI som matcher `.env`, for eksempel:

```text
http://10.0.0.26:8080/spotify/callback
```

## Start lokalt

```bash
source .venv/bin/activate
export $(grep -v '^#' .env | xargs)
python app.py
```

Åpne så:

```text
http://10.0.0.26:8080
```

## systemd

Eksempelfil ligger i `pinyl-web.service`.

```bash
sudo cp pinyl-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable pinyl-web
sudo systemctl start pinyl-web
```

## Lagring

Appen lagrer data i:

- `data/cards.json`
- `data/state.json`
- `data/.spotify_cache`
