"""
Periodically queries Google Maps location sharing (RPC Qt4aBe).

Instead of hard-copied cookies, a dedicated Chromium browser runs Google
Maps. This browser rotates the session cookies itself, and the query always
uses its current cookies.

Locations are published via MQTT (settings in config.toml, can be disabled
with enabled = false in the [mqtt] section).

Setup:
    pip install playwright paho-mqtt
    playwright install chromium

In the DevTools, choose "Copy as cURL (bash)" on the Qt4aBe request and paste
the complete command unchanged into the file curl.txt in the same folder.

Run without a window: python location_bridge.py --headless
(or headless = true in config.toml)

The cookies are imported if the profile does not contain a login yet.
After that they live in the "profile" folder and are kept current by the browser.
"""
import json
import re
import shlex
import signal
import socket
import ssl
import sys
import time
import tomllib
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlsplit, urlunsplit

import paho.mqtt.client as mqtt
from playwright.sync_api import Error, sync_playwright

PROFILE = Path("profile")
LEGACY_PROFILE = Path("profil")  # folder name before the English migration
CONFIG = Path("config.toml")
CURL = Path("curl.txt")
# Remembers which version of curl.txt was last imported into the profile
IMPORT_MARKER = PROFILE / ".curl_import"
# Files in the profile that are deleted at every browser start (history, not needed)
HISTORY_FILES = ("History", "History-journal", "Visited Links", "Top Sites", "Top Sites-journal")
# Scheduled browser restart: attempts and pause between them before giving up
RESTART_ATTEMPTS = 3
RESTART_RETRY_SECONDS = 30

# MQTT/Home Assistant contract: these values are deliberately kept unchanged
# (German) so existing subscribers, automations and HA entities keep working.
SESSION_SUBTOPIC = "sitzung"
LAST_QUERY_SUBTOPIC = "letzte_abfrage"
SESSION_PAYLOAD_OK = "ok"
SESSION_PAYLOAD_EXPIRED = "abgelaufen"

# Last seen at token (anti-CSRF), updated continuously
at_token = {"value": None}

# Session-dependent URL parameters that Google Maps itself is currently using
SESSION_PARAMETERS = ("f.sid", "bl")
url_parameters = {}


def now():
    return f"{datetime.now():%H:%M:%S}"


def at_from_request(request):
    """Reads the at token and session parameters from requests made by Google Maps itself."""
    if request.method != "POST" or "batchexecute" not in request.url:
        return
    query = parse_qs(urlsplit(request.url).query)
    for name in SESSION_PARAMETERS:
        if query.get(name, [""])[0]:
            url_parameters[name] = query[name][0]
    try:
        data = request.post_data
    except Exception:
        return
    if data and "at=" in data:
        value = parse_qs(data).get("at", [None])[0]
        if value:
            at_token["value"] = value


def at_from_page(page):
    """Tries to read the at token directly from the page data."""
    try:
        return page.evaluate("() => window.WIZ_global_data && window.WIZ_global_data.SNlM0e")
    except Exception:
        return None


def update_url(url):
    """Inserts current f.sid/bl and increments _reqid, just like the page itself does."""
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    new = []
    for name, value in pairs:
        if name in url_parameters:
            value = url_parameters[name]
        elif name == "_reqid" and value.isdigit():
            value = str(int(value) + 100000)
        new.append((name, value))
    return urlunsplit(parts._replace(query=urlencode(new)))


def insert_at(body, value):
    return re.sub(r"(^|&)at=[^&]*", lambda m: f"{m.group(1)}at={quote(value, safe='')}", body)


def _resolve_ansi_c(text):
    """Converts bash strings of the form $'...' into normally quoted strings."""
    def replace(m):
        content = m.group(1).encode("utf-8").decode("unicode_escape")
        try:
            content = content.encode("latin-1").decode("utf-8")  # \xNN bytes as UTF-8
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        return shlex.quote(content)
    return re.sub(r"\$'((?:[^'\\]|\\.)*)'", replace, text)


def read_curl(path=CURL):
    """Reads URL, body and cookie string from a copied curl command."""
    text = Path(path).read_text(encoding="utf-8")
    text = text.replace("\\\r\n", " ").replace("\\\n", " ")  # line continuations
    parts = shlex.split(_resolve_ansi_c(text))

    url, body, cookie_string = None, None, None
    with_value = {"-X", "--request", "-A", "--user-agent", "-e", "--referer", "-u", "--user"}
    i = 1  # parts[0] is "curl"
    while i < len(parts):
        t = parts[i]
        if t in ("-H", "--header"):
            name, _, value = parts[i + 1].partition(":")
            if name.strip().lower() == "cookie":
                cookie_string = value.strip()
            i += 2
        elif t in ("-b", "--cookie"):
            cookie_string = parts[i + 1]
            i += 2
        elif t in ("--data-raw", "--data", "-d", "--data-binary"):
            body = parts[i + 1]
            i += 2
        elif t in with_value:
            i += 2
        elif t.startswith("http"):
            url = t
            i += 1
        else:
            i += 1  # flag without value, e.g. --compressed

    missing = [n for n, v in (("URL", url), ("body", body), ("cookies", cookie_string)) if not v]
    if missing:
        sys.exit(f"Not found in {path}: {', '.join(missing)}. "
                 "Was the complete command of the Qt4aBe request copied?")
    return url, body, cookie_string


def parse_cookies(cookie_string):
    cookies = []
    for part in cookie_string.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        # Without "expires" these would be session cookies that Chromium discards on exit
        c = {"name": name, "value": value, "secure": True,
             "expires": int(time.time()) + 365 * 24 * 3600}
        if name.startswith("__Host-"):
            c["url"] = "https://www.google.com"  # __Host- cookies must not have a domain
        else:
            c["domain"] = ".google.com"
            c["path"] = "/"
        cookies.append(c)
    return cookies


def curl_version():
    """Modification time of curl.txt (ns), used to detect a newly placed file."""
    try:
        return CURL.stat().st_mtime_ns
    except OSError:
        return 0


def last_import():
    try:
        return int(IMPORT_MARKER.read_text().strip())
    except (OSError, ValueError):
        return None


def remember_import(version):
    try:
        IMPORT_MARKER.parent.mkdir(parents=True, exist_ok=True)
        IMPORT_MARKER.write_text(str(version))
    except OSError as e:
        print(f"Could not save import version: {e}")


def import_cookies(ctx, cookie_string, version, reason):
    """Replaces all cookies in the profile with those from curl.txt."""
    print(f"[{now()}] {reason}, importing cookies from {CURL}")
    ctx.clear_cookies()
    ctx.add_cookies(parse_cookies(cookie_string))
    remember_import(version)
    # Discard values of the old session
    at_token["value"] = None
    url_parameters.clear()


def parse_response(text):
    """Looks for the Qt4aBe block and returns the inner data structure."""
    for line in text.splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, list):
            continue  # length prefixes like "1610" are plain numbers
        for entry in data:
            if (isinstance(entry, list) and len(entry) > 2
                    and entry[:2] == ["wrb.fr", "Qt4aBe"] and entry[2]):
                try:
                    return json.loads(entry[2])
                except (TypeError, ValueError):
                    print("Qt4aBe response does not contain valid JSON")
                    return None
    return None


def check_legacy_setup(config):
    """Stops with a hint if config or profile still use the old (German) names."""
    mqtt_section = config.get("mqtt", {})
    if "allgemein" in config or any(k in mqtt_section for k in ("aktiv", "benutzer", "passwort", "ca_datei")) \
            or "praefix" in config.get("homeassistant", {}):
        sys.exit(f"{CONFIG} still uses the old German keys. "
                 "Please run: python migrate_config.py <data folder>")
    if LEGACY_PROFILE.is_dir() and not PROFILE.exists():
        sys.exit(f"Found old profile folder '{LEGACY_PROFILE}' but no '{PROFILE}'. "
                 "Please run: python migrate_config.py <data folder>")


def parse_window_size(value):
    """'800x600' -> (800, 600); empty string = Playwright default (1280x720)."""
    if not value:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", str(value))
    if not m or int(m.group(1)) < 100 or int(m.group(2)) < 100:
        sys.exit(f'[general] window_size must look like "800x600" (at least 100x100), got: {value!r}')
    return int(m.group(1)), int(m.group(2))


def load_config():
    if not CONFIG.exists():
        sys.exit(f"{CONFIG} is missing. Please copy the example config.toml next to it and adjust it.")
    with CONFIG.open("rb") as f:
        c = tomllib.load(f)
    check_legacy_setup(c)
    general = c.get("general", {})
    m = c.get("mqtt", {})
    ha = c.get("homeassistant", {})
    enabled = bool(m.get("enabled", True))
    if enabled and not m.get("host"):
        sys.exit("[mqtt] host is missing in config.toml (or set enabled = false).")
    return {
        "interval": int(general.get("interval_seconds", 300)),
        "expired_after": max(1, int(general.get("expired_after_failures", 3))),
        "exit_after": max(0, int(general.get("exit_after_expired_rounds", 0))),
        "headless": bool(general.get("headless", False)) or "--headless" in sys.argv,
        "idle_blank": bool(general.get("idle_blank", True)),
        "cache_mb": max(0, int(general.get("cache_size_mb", 100))),
        "restart_hours": max(0.0, float(general.get("browser_restart_hours", 2))),
        "reload_minutes": max(0.0, float(general.get("page_reload_minutes", 15))),
        "window_size": parse_window_size(general.get("window_size", "800x600")),
        "disable_webgl": bool(general.get("disable_webgl", True)),
        "mqtt_enabled": enabled,
        "host": m.get("host", ""),
        "port": int(m.get("port", 1883)),
        "username": m.get("username", ""),
        "password": m.get("password", ""),
        "client_id": m.get("client_id", "google-standorte"),
        "tls": bool(m.get("tls", False)),
        "ca_file": m.get("ca_file", ""),
        "tls_insecure": bool(m.get("tls_insecure", False)),
        "debug": bool(m.get("debug", False)) or "--mqtt-debug" in sys.argv,
        "qos": int(m.get("qos", 1)),
        "retain": bool(m.get("retain", True)),
        "topic": m.get("topic", "standorte").rstrip("/"),
        "ha_discovery": bool(ha.get("discovery", False)),
        "ha_prefix": ha.get("prefix", "homeassistant").rstrip("/"),
    }


def slug(text):
    """'André' -> 'andre', 'Leslie P.' -> 'leslie_p' (for topics)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "unknown"


def read_people(inner):
    """Turns the Google structure into a list of simple dicts."""
    result = []
    for p in (inner[0] if inner else None) or []:
        try:
            loc = p[1]
            if loc[1][2] is None or loc[1][1] is None or loc[2] is None:
                print(f"Entry skipped, no coordinates for {p[0][3]}")
                continue
            battery = p[13] if len(p) > 13 and p[13] else []
            result.append({
                "id": p[0][0],
                "name": p[0][3],
                "latitude": loc[1][2],
                "longitude": loc[1][1],
                "gps_accuracy": loc[3],
                "last_seen": datetime.fromtimestamp(loc[2] / 1000, timezone.utc).astimezone().isoformat(),
                "last_seen_ms": loc[2],
                "age_seconds": round(time.time() - loc[2] / 1000),
                "address": loc[4],
                "battery_level": battery[1] if len(battery) > 1 else None,
                "charging": battery[0] if len(battery) > 0 else None,
            })
        except (IndexError, TypeError) as e:
            print(f"Entry skipped, unexpected format: {e}")
    return result


def tls_context(c):
    ctx = ssl.create_default_context(cafile=c["ca_file"] or None)
    if c["tls_insecure"]:
        # Workaround for too weak certificates: no verification, OpenSSL security level 0
        print("WARNING: tls_insecure = true, the MQTT broker's certificate is NOT verified")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    return ctx


class Mqtt:
    def __init__(self, c):
        self.c = c
        self.status_topic = f"{c['topic']}/LWT"  # Last Will: "online" / "offline"
        self.session_topic = f"{c['topic']}/{SESSION_SUBTOPIC}"
        self.known = {}  # Google ID -> name, for Home Assistant discovery
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=c["client_id"])
        if c["username"]:
            self.client.username_pw_set(c["username"], c["password"] or None)
        if c["tls"]:
            if c["ca_file"] and not Path(c["ca_file"]).is_file():
                sys.exit(f"[mqtt] ca_file not found: {c['ca_file']}")
            self.client.tls_set_context(tls_context(c))
        # Last will: broker reports "offline" if the script dies unexpectedly
        self.client.will_set(self.status_topic, "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        if c["debug"]:
            self.client.on_connect_fail = self._on_connect_fail
            self.client.on_publish = self._on_publish
            self.client.on_log = self._log
            self._debug(f"Connecting to {c['host']}:{c['port']} (client_id={c['client_id']}, "
                        f"username={c['username'] or '-'}, tls={c['tls']}, qos={c['qos']}, "
                        f"retain={c['retain']}, topic={c['topic']})")
        self.client.connect_async(c["host"], c["port"])
        self.client.loop_start()  # connects in the background and reconnects if needed

    def _debug(self, text):
        if self.c["debug"]:
            print(f"[MQTT debug {now()}] {text}")

    def _log(self, client, userdata, level, buf):
        self._debug(f"paho: {buf}")

    def _on_connect_fail(self, client, userdata):
        self._debug(f"Connection to {self.c['host']}:{self.c['port']} failed "
                    "- testing TCP/TLS directly to show the cause ...")
        host, port = self.c["host"], self.c["port"]
        try:
            sock = socket.create_connection((host, port), timeout=5)
        except Exception as e:
            self._debug(f"TCP test failed: {type(e).__name__}: {e}")
            return
        self._debug("TCP test ok (port reachable)")
        if not self.c["tls"]:
            sock.close()
            return
        try:
            with tls_context(self.c).wrap_socket(sock, server_hostname=host) as s:
                self._debug(f"TLS test ok: {s.version()}, certificate: {s.getpeercert().get('subject')}")
        except Exception as e:
            self._debug(f"TLS test failed: {type(e).__name__}: {e}")
            self._debug("Note: with a self-signed certificate or a missing IP in the certificate, "
                        "verification against the default CAs fails")

    def _on_publish(self, client, userdata, mid, reason_code, properties):
        self._debug(f"Publish acknowledged (mid={mid}, {reason_code})")

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self._debug(f"on_connect: reason_code={reason_code}, flags={flags}")
        if reason_code.is_failure:
            print(f"MQTT connection refused: {reason_code}")
            return
        print(f"MQTT connected to {self.c['host']}:{self.c['port']}")
        client.publish(self.status_topic, "online", qos=1, retain=True)
        self._session_discovery()
        # Copy, because the main thread may add new people in parallel
        for gid, name in list(self.known.items()):
            self._discovery(gid, name)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._debug(f"on_disconnect: reason_code={reason_code}")
        if reason_code != 0:
            print(f"MQTT connection lost ({reason_code}), retrying ...")

    def _discovery(self, gid, name):
        if not self.c["ha_discovery"]:
            return
        config = {
            "name": name,
            # unique_id unchanged so Home Assistant keeps the existing entities
            "unique_id": f"google_standort_{gid}",
            "json_attributes_topic": f"{self._person_topic(gid)}/status",
            "source_type": "gps",
            "availability_topic": self.status_topic,
        }
        self.client.publish(f"{self.c['ha_prefix']}/device_tracker/google_{gid}/config",
                            json.dumps(config), qos=1, retain=True)

    def _session_discovery(self):
        """Binary sensor in HA that switches to "problem" when the Google session has expired."""
        if not self.c["ha_discovery"]:
            return
        # unique_id/topic unchanged so Home Assistant keeps the existing entity
        object_id = f"{slug(self.c['client_id'])}_{SESSION_SUBTOPIC}"
        config = {
            "name": "Google session expired",
            "unique_id": object_id,
            "state_topic": self.session_topic,
            "payload_on": SESSION_PAYLOAD_EXPIRED,
            "payload_off": SESSION_PAYLOAD_OK,
            "device_class": "problem",
            "availability_topic": self.status_topic,
        }
        self.client.publish(f"{self.c['ha_prefix']}/binary_sensor/{object_id}/config",
                            json.dumps(config), qos=1, retain=True)

    def session(self, ok):
        self.client.publish(self.session_topic, SESSION_PAYLOAD_OK if ok else SESSION_PAYLOAD_EXPIRED,
                            qos=1, retain=True)

    def _person_topic(self, gid):
        # Google ID instead of name: unique and stable, even with identical or changed names
        return f"{self.c['topic']}/{slug(str(gid))}"

    def person(self, data):
        if self.known.get(data["id"]) != data["name"]:
            # new or renamed: (re)send discovery so HA knows the name
            self.known[data["id"]] = data["name"]
            self._discovery(data["id"], data["name"])
        base = self._person_topic(data["id"])
        # individual fields: topic/USERID/name, /latitude, /longitude, ...
        for field, value in data.items():
            if value is None:
                text = ""
            elif isinstance(value, bool):
                text = "true" if value else "false"
            else:
                text = str(value)
            self.client.publish(f"{base}/{field}", text, qos=self.c["qos"], retain=self.c["retain"])
        # complete JSON structure under topic/USERID/status
        topic = f"{base}/status"
        info = self.client.publish(topic, json.dumps(data, ensure_ascii=False),
                                   qos=self.c["qos"], retain=self.c["retain"])
        self._debug(f"publish {topic} (mid={info.mid}, rc={info.rc}, connected={self.client.is_connected()})")

    def query_result(self, ok, count, message):
        # JSON keys unchanged (MQTT contract)
        data = {"ok": ok, "personen": count, "meldung": message,
                "zeit": datetime.now().astimezone().isoformat(timespec="seconds")}
        self.client.publish(f"{self.c['topic']}/{LAST_QUERY_SUBTOPIC}",
                            json.dumps(data, ensure_ascii=False), qos=1, retain=True)

    def close(self):
        try:
            self.client.publish(self.status_topic, "offline", qos=1, retain=True).wait_for_publish(2)
        except Exception:
            pass
        self.client.disconnect()
        self.client.loop_stop()


class NoMqtt:
    """Placeholder for enabled = false: does nothing, locations only appear on the console."""

    def person(self, data):
        pass

    def query_result(self, ok, count, message):
        pass

    def session(self, ok):
        pass

    def close(self):
        pass


def process(people, broker):
    timestamp = now()
    if not people:
        print(f"[{timestamp}] Empty response – check session or at token")
        broker.query_result(False, 0, "Empty response – check session or at token")
        return 0
    for d in people:
        seen = datetime.fromisoformat(d["last_seen"]).strftime("%H:%M:%S")
        print(f"[{timestamp}] {d['name']}: {d['latitude']:.6f}, {d['longitude']:.6f} "
              f"(±{d['gps_accuracy']} m, as of {seen}, battery {d['battery_level']} %)")
        broker.person(d)
    broker.query_result(True, len(people), "ok")
    return len(people)


class SessionWatchdog:
    """Detects an expired Google session and reports it.

    An empty response alone is no proof (maybe nobody is sharing right now),
    so only clear indications count, and only above a threshold is it considered expired.
    """

    def __init__(self, c, broker):
        self.threshold = c["expired_after"]
        self.exit_after = c["exit_after"]
        self.broker = broker
        self.suspicion = 0
        self.expired = False
        self.expired_rounds = 0
        self.ok_reported = False

    def success(self):
        if self.expired:
            print(f"[{now()}] Google session is valid again")
        if self.expired or not self.ok_reported:
            self.broker.session(True)
            self.ok_reported = True
        self.suspicion = 0
        self.expired = False
        self.expired_rounds = 0

    def indication(self, reason, certain=False):
        self.suspicion = self.threshold if certain else self.suspicion + 1
        timestamp = now()
        if self.suspicion < self.threshold:
            print(f"[{timestamp}] Indication of expired session ({reason}), {self.suspicion}/{self.threshold}")
            return
        message = (f"Google session expired ({reason}) – please log in again and "
                   f"place a fresh {CURL}")
        if not self.expired:
            self.expired = True
            self.ok_reported = False
            print(f"[{timestamp}] !!! {message}")
            self.broker.session(False)
        self.broker.query_result(False, 0, message)

    def reimported(self):
        # Fresh cookies get a new chance; "expired" stays until the first success
        self.suspicion = 0
        self.expired_rounds = 0

    def round_finished(self):
        if not self.expired:
            return
        self.expired_rounds += 1
        print(f"[{now()}] Session still expired, waiting for a new {CURL}")
        if self.exit_after and self.expired_rounds >= self.exit_after:
            print(f"Session expired for {self.expired_rounds} rounds, exiting with code 2")
            raise SystemExit(2)


def browser_closed(error, ctx):
    """True if window/browser are really closed (not just a network error)."""
    if "has been closed" in str(error):
        return True
    try:
        return not ctx.pages
    except Error:
        return True


def report_error(error, broker):
    message = f"{type(error).__name__}: {str(error).splitlines()[0] if str(error) else ''}"
    print(f"[{now()}] Query failed, retrying in the next interval – {message}")
    try:
        broker.query_result(False, 0, message)
    except Exception:
        pass


def open_browser(pw, c):
    """Starts Chromium with the persistent profile and returns (context, page)."""
    # Browsing history is not needed (the login lives in the cookies) and only grows,
    # since Maps is loaded in every round: delete it while Chromium is not running
    for name in HISTORY_FILES:
        try:
            (PROFILE / "Default" / name).unlink(missing_ok=True)
        except OSError as e:
            print(f"Could not delete {name}: {e}")
    args = []
    if c["cache_mb"]:
        # Limits Chromium's HTTP disk cache in the profile (otherwise it grows to several GB)
        args.append(f"--disk-cache-size={c['cache_mb'] * 1024 * 1024}")
    if c["disable_webgl"]:
        # Without WebGL Maps falls back to its 2D "lite mode", which is much cheaper
        # than 3D rendering in software (no GPU in the container)
        args.append("--disable-3d-apis")
    options = {}
    if c["window_size"]:
        # Small window = fewer pixels to render and fewer map tiles to load
        width, height = c["window_size"]
        options["viewport"] = {"width": width, "height": height}
    ctx = pw.chromium.launch_persistent_context(str(PROFILE), headless=c["headless"], args=args, **options)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.on("request", at_from_request)
    return ctx, page


def restart_browser(pw, c, broker):
    """Starts Chromium again after a scheduled restart, with a few retries.

    A failed start must not end up in the "browser closed" handler (which would exit
    with code 0). After the last failed attempt exit with code 1, so Docker restarts.
    """
    for attempt in range(1, RESTART_ATTEMPTS + 1):
        try:
            return open_browser(pw, c)
        except Error as e:
            report_error(e, broker)
            if attempt == RESTART_ATTEMPTS:
                print(f"Browser could not be restarted ({attempt} attempts), exiting with code 1")
                raise SystemExit(1)
            print(f"Browser restart failed (attempt {attempt}), retrying in {RESTART_RETRY_SECONDS} s")
            time.sleep(RESTART_RETRY_SECONDS)


def load_maps(page, ctx):
    """Loads Google Maps: keeps the session alive, lets the browser rotate cookies and
    provides a fresh at token. Returns (problem, page_at); problem is None if logged in."""
    page.goto("https://www.google.com/maps")
    page.wait_for_timeout(5000)
    if "accounts.google.com" in page.url:
        return "redirect to Google login", None
    if "SID" not in {ck["name"] for ck in ctx.cookies("https://www.google.com")}:
        return "login cookie SID missing", None
    return None, at_from_page(page)


def _sigterm(signum, frame):
    # "docker stop" sends SIGTERM: exit as cleanly as with Ctrl+C
    raise KeyboardInterrupt


def main():
    signal.signal(signal.SIGTERM, _sigterm)
    c = load_config()
    version = curl_version()  # before reading, so a change in the meantime is not lost
    url, body, cookie_string = read_curl()
    if c["mqtt_enabled"]:
        broker = Mqtt(c)
    else:
        print("MQTT is disabled (enabled = false), locations on the console only")
        broker = NoMqtt()

    with sync_playwright() as pw:
        ctx, page = open_browser(pw, c)
        browser_started = time.monotonic()
        existing = {ck["name"] for ck in ctx.cookies("https://www.google.com")}
        marker = last_import()
        if "SID" not in existing:
            import_cookies(ctx, cookie_string, version, "No login cookies in profile")
        elif marker is None:
            remember_import(version)  # existing profile from an older version: treat as current
        elif version > marker:
            import_cookies(ctx, cookie_string, version, f"{CURL} is newer than the last import")

        session = SessionWatchdog(c, broker)
        need_reload = True   # load Maps in the next round regardless of the schedule
        last_reload = 0.0    # time of the last Maps load (monotonic)
        page_at = None       # at token read from the page at the last Maps load
        on_maps = False      # the tab currently shows Maps (not about:blank)

        def query():
            """Sends the Qt4aBe request. Returns (response, people)."""
            nonlocal url, body
            new_at = page_at or at_token["value"]
            if new_at:
                if new_at not in body and quote(new_at, safe="") not in body:
                    print(f"[{now()}] New at token applied")
                body = insert_at(body, new_at)
            else:
                print(f"[{now()}] No fresh at token found, using the old one")
            url = update_url(url)
            response = ctx.request.post(
                url,
                data=body,
                headers={
                    "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "x-same-domain": "1",
                    "origin": "https://www.google.com",
                    "referer": "https://www.google.com/",
                },
            )
            people = read_people(parse_response(response.text())) if response.ok else []
            return response, people

        def reload_maps():
            nonlocal need_reload, last_reload, page_at, on_maps
            on_maps = True
            need_reload = False
            last_reload = time.monotonic()
            problem, page_at = load_maps(page, ctx)
            return problem

        try:
            while True:
                loaded = False  # Maps was loaded in this round
                try:
                    # Expired and a new curl.txt placed? Then apply it without restart
                    if session.expired and curl_version() > (last_import() or 0):
                        new_version = curl_version()
                        try:
                            url, body, cookie_string = read_curl()
                        except SystemExit as e:
                            print(f"New {CURL} incomplete, trying again later: {e}")
                        else:
                            import_cookies(ctx, cookie_string, new_version, f"New {CURL} found")
                            session.reimported()
                            need_reload = True

                    # Loading Maps keeps the session alive, lets the browser rotate cookies and
                    # provides a fresh at token. It is the expensive part of a round, so it is
                    # only done every reload_minutes; the queries in between reuse the token.
                    problem = None
                    if (need_reload or not c["reload_minutes"]
                            or time.monotonic() - last_reload >= c["reload_minutes"] * 60):
                        loaded = True
                        problem = reload_maps()

                    if not problem:
                        response, people = query()
                        if not loaded and not (response.ok and people):
                            # Maybe the reused token is no longer accepted: reload Maps and
                            # retry once before the session watchdog counts it
                            print(f"[{now()}] Query without fresh Maps load failed, "
                                  "reloading Maps and retrying")
                            loaded = True
                            problem = reload_maps()
                            if not problem:
                                response, people = query()

                    if problem:
                        session.indication(problem, certain=True)
                    elif not response.ok:
                        message = f"HTTP {response.status} {response.status_text}".strip()
                        print(f"[{now()}] Query failed: {message}")
                        broker.query_result(False, 0, message)
                        if response.status in (401, 403):
                            session.indication(message)
                    elif process(people, broker):
                        session.success()
                    elif not page_at:
                        # empty AND the page provides no at token: typical for "not logged in"
                        session.indication("empty response, page without at token")

                except Error as e:
                    if browser_closed(e, ctx):
                        raise
                    report_error(e, broker)
                except Exception as e:
                    # e.g. unexpected response format: log it, retry in the next interval
                    report_error(e, broker)

                session.round_finished()
                idle_start = time.monotonic()

                # A long-running Chromium slowly accumulates CPU load: restart it regularly,
                # here between two queries so the query itself is not delayed.
                # Login cookies live in the profile and survive the restart.
                if c["restart_hours"] and idle_start - browser_started > c["restart_hours"] * 3600:
                    print(f"[{now()}] Restarting browser (every {c['restart_hours']:g} h)")
                    try:
                        ctx.close()
                    except Error:
                        pass
                    ctx, page = restart_browser(pw, c, broker)
                    browser_started = time.monotonic()
                    on_maps = False  # the new tab starts empty
                    # Normally the login survives in the profile; if not, restore it like at startup
                    if "SID" not in {ck["name"] for ck in ctx.cookies("https://www.google.com")}:
                        import_cookies(ctx, cookie_string, last_import() or curl_version(),
                                       "Login cookies missing after browser restart")
                        need_reload = True

                # Google Maps keeps rendering (WebGL in software, no GPU in the container) and
                # causes permanent CPU load. Right after loading it, park the tab on an empty
                # page until the next Maps load.
                if c["idle_blank"] and on_maps:
                    try:
                        page.goto("about:blank")
                        on_maps = False
                    except Error as e:
                        if browser_closed(e, ctx):
                            raise

                # wait_for_timeout instead of time.sleep so the browser keeps working;
                # the time of a browser restart counts towards the interval
                remaining = c["interval"] - (time.monotonic() - idle_start)
                page.wait_for_timeout(max(0, remaining) * 1000)

        except KeyboardInterrupt:
            print("\nStopped (Ctrl+C)")
        except Error as e:
            if browser_closed(e, ctx):
                print("Browser closed, exiting")
            else:
                raise
        finally:
            broker.close()
            try:
                ctx.close()
            except Error:
                pass  # was already closed


if __name__ == "__main__":
    main()
