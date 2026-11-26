# Google Location Bridge

Periodically reads **Google Maps location sharing** (locations shared by friends/family) and publishes it via **MQTT**. Optionally, the people are created automatically as `device_tracker` entities in **Home Assistant** via MQTT discovery.

## How it works

Google offers no official API for shared locations. The project therefore starts its own Chromium browser (Playwright) with a persistent profile:

1. On the first start, the login cookies from the file `curl.txt` are imported into the browser profile.
2. In every interval the browser reloads Google Maps and thereby keeps the session alive (Google rotates the cookies itself, they no longer need to be maintained manually).
3. With the current cookies and the fresh `at` token, the location query (RPC `Qt4aBe`) is sent and evaluated.
4. The results are sent to the broker via MQTT (or only to the console if MQTT is disabled).
5. If the session expires, this is reported (`standorte/sitzung` = `abgelaufen`). A newly placed `curl.txt` is then picked up **without a restart**.

### MQTT topics (base topic `standorte`)

| Topic | Content |
|---|---|
| `standorte/<google-id>/<field>` | Individual values per person (`name`, `latitude`, `longitude`, ...) |
| `standorte/<google-id>/status` | Complete JSON structure of the person |
| `standorte/sitzung` | `ok` or `abgelaufen` (expired) |
| `standorte/letzte_abfrage` | JSON with result and timestamp of the last query (`ok`, `personen`, `meldung`, `zeit`) |
| `standorte/LWT` | Last will: `online` / `offline` |

The topic names and payloads are kept unchanged for compatibility with existing setups.

## Requirements

- Docker (or Podman) with Compose, or alternatively Python 3.12 for running locally
- A Google account that sees location sharing in Google Maps
- Optional: MQTT broker (e.g. Mosquitto), Home Assistant

## Step 1: Create `curl.txt` (login)

1. Open Google Maps in the browser (logged in) and open the developer tools (F12, **Network** tab).
2. Reload the page and filter for a request with **`Qt4aBe`** (`batchexecute?rpcids=Qt4aBe...`).
3. Right-click the request: **Copy > Copy as cURL (bash)**.
4. Paste the complete command unchanged into the file `data/curl.txt`.

> `curl.txt` contains login cookies and must be treated like a password. It is listed in `.gitignore` and must not be committed.

## Step 2: Configuration

```bash
cp data/config.example.toml data/config.toml
```

Adjust `data/config.toml`. The most important settings:

| Section | Key | Meaning |
|---|---|---|
| `[general]` | `interval_seconds` | Query interval (default in the example: 120) |
| `[general]` | `headless` | Chromium without a window (not needed in the container, Xvfb runs there) |
| `[general]` | `idle_blank` | `true` (default): Google Maps is replaced by an empty tab between queries. Maps would otherwise keep rendering in software and cause constant CPU load. |
| `[general]` | `cache_size_mb` | Maximum size of Chromium's disk cache in the profile (default: 100). `0` = Chromium default without a fixed limit. |
| `[general]` | `expired_after_failures` | Number of indications in a row after which the session is considered expired |
| `[general]` | `exit_after_expired_rounds` | `>0`: program exits with code 2, `0`: keep running and wait for a new `curl.txt` |
| `[mqtt]` | `enabled`, `host`, `port`, `username`, `password` | MQTT connection (`enabled = false` = console output only) |
| `[mqtt]` | `tls`, `ca_file`, `tls_insecure` | TLS; `ca_file` e.g. `/data/ca.crt` for an own CA |
| `[mqtt]` | `topic`, `qos`, `retain` | Base topic and delivery |
| `[homeassistant]` | `discovery`, `prefix` | Home Assistant discovery |

In the container all files are located under `/data`; paths in the configuration (e.g. `ca_file`) must refer to it.

## Upgrading from an older (German) version

Config keys and the profile folder were renamed to English (`[allgemein]` → `[general]`, `aktiv` → `enabled`, `profil/` → `profile/`, ...). The script `migrate_config.py` converts an existing setup, keeps all values and comments and writes a backup `config.toml.bak`:

```bash
python3 migrate_config.py data --dry-run   # show what would change
python3 migrate_config.py data
```

The Docker container runs the migration automatically on every start (it does nothing if the setup is already migrated). If the program is started with an unmigrated setup, it stops with a hint.

## Installation with Docker Compose

```bash
git clone <repo-url> google-location-bridge
cd google-location-bridge
mkdir -p data
# create data/curl.txt and data/config.toml as described above
sudo chown -R 1000:1000 data   # Linux: the container runs as UID 1000
docker compose up -d --build
docker compose logs -f
```

The folder `./data` is mounted into the container as `/data` and contains:

- `config.toml` and `curl.txt` (must exist, otherwise the start is aborted)
- `profile/` (Chromium profile with the session cookies, created automatically)

The container starts Chromium with a virtual display (Xvfb). Important are `shm_size: 1gb` (Chromium needs more than the default 64 MB) and `init: true` (reaps Chromium processes).

## Installation with Portainer

The file [docker-compose_portainer.yml](docker-compose_portainer.yml) uses a **pre-built image** (`localhost/google-location-bridge_google-standorte:latest`) and a fixed host path. Procedure:

1. **On the Docker host**, clone the project and build the image once:
   ```bash
   git clone <repo-url> /home/gloc/google-location-bridge
   cd /home/gloc/google-location-bridge
   docker compose build
   ```
   If the resulting image has a different name, adjust it in `docker-compose_portainer.yml` (`docker images` shows it). With Podman the name gets the prefix `localhost/`.
2. **Create the data folder** (outside of Git):
   ```bash
   mkdir -p /home/gloc/google-location-bridge/data
   ```
   Place `curl.txt` and `config.toml` there (see steps 1 and 2). The container runs as UID 1000, so the folder must belong to it:
   ```bash
   sudo chown -R 1000:1000 /home/gloc/google-location-bridge/data
   ```
3. In **Portainer**, create a new stack under **Stacks > Add stack** (name e.g. `google-standorte`), paste the content of `docker-compose_portainer.yml` into the **Web editor** and adjust the host path `/home/gloc/google-location-bridge/data` if necessary.
4. Click **Deploy the stack**. The logs are available under **Containers > google-standorte > Logs**.

Notes:

- `restart: always` makes sure the container starts automatically after a reboot.
- `:Z` on the volume sets the SELinux label (required e.g. with Podman/Fedora/RHEL). On systems without SELinux it can be omitted.
- **Hardening**: the container runs as unprivileged user (UID 1000) without Linux capabilities (`cap_drop: ALL`), with `no-new-privileges` and limits for processes and memory. Chromium itself runs without its own sandbox (`--no-sandbox`, Playwright default in containers), so these restrictions matter especially with rootful Podman/Docker.
- **Update**: fetch the code with `git pull`, run `docker compose build`, then redeploy the stack in Portainer or recreate the container.

## Operation: session expired

When the Google session becomes invalid (log message, `standorte/sitzung` = `abgelaufen`, in Home Assistant the sensor "Google session expired"):

1. Create a **new `curl.txt`** as in step 1.
2. Replace the file in `data/`. The running container picks it up automatically in the next run, a restart is not necessary (with `exit_after_expired_rounds = 0`).

## Running locally without Docker

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# config.toml and curl.txt are in the current folder, not in data/
python location_bridge.py      # options: --headless, --mqtt-debug
```

The script expects `config.toml`, `curl.txt` and the folder `profile/` in the **current working directory**.

## Security

- `data/` (profile, `config.toml`, `curl.txt`, certificates) contains credentials and is excluded via `.gitignore`. Only `data/config.example.toml` is versioned.
- The Chromium profile contains a valid Google session. Restrict access to the data folder accordingly.
