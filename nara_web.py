# usage: python nara_web.py --host 0.0.0.0 --port 8888 --adb-device emulator-5554

import argparse
from datetime import date, timedelta
import hashlib
import hmac
import html
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, Optional, cast
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from nara_live_export import (
    REMOTE_FIREBASE_DB,
    REMOTE_NARA_DB,
    adb_pull,
    collect_live_data,
)


GLOBAL_CSS = """
@import url("https://fonts.googleapis.com/css2?family=Mystery+Quest&family=Slackey&display=swap");
:root {
  --font-body: "Mystery Quest", "Noto Sans", cursive;
  --font-display: "Slackey", "Mystery Quest", cursive;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  background: #0b0b0b;
  color: #f2f2f2;
  font-family: var(--font-body);
}
""".strip()

POOP_ALERT_THRESHOLD_MS = 2 * 24 * 60 * 60 * 1000
ALERT_ICON_HTML = "&#9888;&#65039;"
DIAPER_PLOT_MODES = ("all", "dirty", "wet", "dry")
ENV_FILE_NAME = ".env"
AUTH_HEADER = "X-NaraGaiden-Password"
AUTH_COOKIE = "naragaiden_auth"
AUTH_STORAGE_KEY = "naragaiden_password"
AUTH_COOKIE_MAX_AGE = 365 * 24 * 60 * 60
AUTH_THROTTLE_BASE_SECONDS = 1.0
AUTH_THROTTLE_MAX_SECONDS = 60.0
AUTH_THROTTLE_RESET_SECONDS = 15 * 60
HTML_SAFE_JSON_RE = re.compile(r'[&<>\u2028\u2029]')


def load_env_file(file_path: Path):
    if not file_path.exists():
        return
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value


def password_digest(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def json_dumps_for_html(value):
    return HTML_SAFE_JSON_RE.sub(
        lambda match: f"\\u{ord(match.group(0)):04x}",
        json.dumps(value, separators=(",", ":")),
    )


def password_matches(password, expected_digest):
    return hmac.compare_digest(password_digest(password), expected_digest)


def authenticate_password_attempt(server, client_key, password, context):
    expected = getattr(server, "password_hash", None)
    if not expected:
        return "authorized", 0.0

    wait_seconds = auth_throttle_wait_seconds(server, client_key)
    if wait_seconds > 0:
        return "rate_limited", wait_seconds

    if password_matches(password, expected):
        clear_auth_failures(server, client_key)
        return "authorized", 0.0

    delay_seconds = register_auth_failure(server, client_key)
    logging.warning(
        "Rejected %s from %s; next attempt allowed in %.1fs",
        context,
        client_key,
        delay_seconds,
    )
    return "rejected", 0.0


def request_cookie(handler, name):
    cookie_header = handler.headers.get("Cookie")
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return None
    morsel = cookie.get(name)
    if morsel is None:
        return None
    return morsel.value


def request_auth_status(handler):
    server = cast(NaraServer, handler.server)
    expected = getattr(server, "password_hash", None)
    if not expected:
        return "authorized", 0.0

    provided_password = handler.headers.get(AUTH_HEADER)
    if provided_password is not None:
        return authenticate_password_attempt(
            server,
            client_address_text(handler),
            provided_password,
            AUTH_HEADER,
        )

    provided_cookie = request_cookie(handler, AUTH_COOKIE)
    if provided_cookie is not None and hmac.compare_digest(provided_cookie, expected):
        return "authorized", 0.0

    return "unauthorized", 0.0


def auth_cookie_header(password_hash):
    return (
        f"{AUTH_COOKIE}={password_hash}; Path=/; Max-Age={AUTH_COOKIE_MAX_AGE}; "
        "HttpOnly; SameSite=Lax"
    )


def cleared_auth_cookie_header():
    return f"{AUTH_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"


def client_address_text(handler):
    client_address = getattr(handler, "client_address", None)
    if not client_address:
        return "unknown"
    host = client_address[0]
    return str(host or "unknown")


def auth_failure_state(server):
    state = getattr(server, "auth_failures", None)
    if state is None:
        state = {}
        server.auth_failures = state
    return state


def prune_auth_failures(server, now=None):
    if now is None:
        now = time.monotonic()
    state = auth_failure_state(server)
    expired_before = now - AUTH_THROTTLE_RESET_SECONDS
    stale_keys = [
        key
        for key, entry in state.items()
        if float(entry.get("blocked_until", 0.0)) <= now
        and float(entry.get("last_failure", 0.0)) < expired_before
    ]
    for key in stale_keys:
        state.pop(key, None)


def auth_throttle_wait_seconds(server, client_key, now=None):
    if now is None:
        now = time.monotonic()
    prune_auth_failures(server, now)
    entry = auth_failure_state(server).get(client_key)
    if not entry:
        return 0.0
    blocked_until = float(entry.get("blocked_until", 0.0))
    return max(0.0, blocked_until - now)


def register_auth_failure(server, client_key, now=None):
    if now is None:
        now = time.monotonic()
    prune_auth_failures(server, now)
    state = auth_failure_state(server)
    entry = state.get(client_key)
    failure_count = 0
    if entry is not None and (now - float(entry.get("last_failure", now))) <= AUTH_THROTTLE_RESET_SECONDS:
        failure_count = int(entry.get("count", 0))
    failure_count += 1
    delay_seconds = min(
        AUTH_THROTTLE_BASE_SECONDS * (2 ** max(0, failure_count - 1)),
        AUTH_THROTTLE_MAX_SECONDS,
    )
    state[client_key] = {
        "count": failure_count,
        "last_failure": now,
        "blocked_until": now + delay_seconds,
    }
    return delay_seconds


def clear_auth_failures(server, client_key):
    auth_failure_state(server).pop(client_key, None)


def format_retry_after_seconds(wait_seconds):
    wait_int = int(wait_seconds)
    if wait_seconds > wait_int:
        wait_int += 1
    return max(1, wait_int)


def build_auth_html(current_path):
    css = "\n".join(
        [
            GLOBAL_CSS,
            """
body {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.auth-card {
  width: min(420px, 100%);
  background: #171717;
  border: 1px solid #2d2d2d;
  border-radius: 18px;
  padding: 24px;
  box-shadow: 0 18px 60px rgba(0, 0, 0, 0.35);
}
.auth-title {
  margin: 0 0 8px;
  font-family: var(--font-display);
  font-size: 1.8rem;
}
.auth-copy {
  margin: 0 0 16px;
  color: #c8c8c8;
  line-height: 1.45;
}
.auth-form {
  display: grid;
  gap: 12px;
}
.auth-input {
  width: 100%;
  border: 1px solid #3d3d3d;
  border-radius: 12px;
  background: #0f0f0f;
  color: #f2f2f2;
  padding: 12px 14px;
  font: inherit;
}
.auth-button {
  border: 0;
  border-radius: 12px;
  background: #c98a2b;
  color: #111111;
  padding: 12px 14px;
  font: inherit;
  font-weight: 700;
  cursor: pointer;
}
.auth-button[disabled] {
  opacity: 0.7;
  cursor: wait;
}
.auth-status {
  min-height: 1.2em;
  color: #f2a7a7;
}
.auth-status.pending {
  color: #d7c78a;
}
""".strip(),
        ]
    )
    script = f"""
    const storageKey = {json_dumps_for_html(AUTH_STORAGE_KEY)};
    const returnPath = {json_dumps_for_html(current_path)};
    const form = document.querySelector(".auth-form");
    const input = document.querySelector(".auth-input");
    const status = document.querySelector(".auth-status");
    const button = document.querySelector(".auth-button");

    function setStatus(message = "", pending = false) {{
      status.textContent = message;
      status.className = pending ? "auth-status pending" : "auth-status";
    }}

    async function authenticate(password, shouldStore) {{
      button.disabled = true;
      setStatus("Checking password...", true);
      try {{
        const response = await fetch("/auth", {{
          method: "POST",
          headers: {{ "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" }},
          body: new URLSearchParams({{ password }}),
          cache: "no-store",
        }});
        if (!response.ok) {{
          if (response.status === 429) {{
            const message = (await response.text()) || "Too many attempts. Try again shortly.";
            localStorage.removeItem(storageKey);
            setStatus(message);
            input.focus();
            input.select();
            return false;
          }}
          localStorage.removeItem(storageKey);
          setStatus("Password not accepted.");
          input.focus();
          input.select();
          return false;
        }}
        if (shouldStore) {{
          localStorage.setItem(storageKey, password);
        }}
        window.location.replace(returnPath);
        return true;
      }} catch (_err) {{
        setStatus("Could not reach the server.");
        return false;
      }} finally {{
        button.disabled = false;
      }}
    }}

    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const password = input.value;
      if (!password) {{
        setStatus("Enter the password.");
        input.focus();
        return;
      }}
      await authenticate(password, true);
    }});

    const savedPassword = localStorage.getItem(storageKey);
    if (savedPassword) {{
      input.value = savedPassword;
      authenticate(savedPassword, true);
    }} else {{
      input.focus();
    }}
    """.strip()
    return f"""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Nara Login</title>
  <style>
    {css}
  </style>
</head>
<body>
  <main class=\"auth-card\">
    <h1 class=\"auth-title\">Nara Gaiden</h1>
    <p class=\"auth-copy\">Enter the server password to view baby data on this device.</p>
    <form class=\"auth-form\">
      <input class=\"auth-input\" type=\"password\" name=\"password\" autocomplete=\"current-password\" placeholder=\"Server password\" />
      <button class=\"auth-button\" type=\"submit\">Unlock</button>
      <div class=\"auth-status\" aria-live=\"polite\"></div>
    </form>
  </main>
  <script>
    {script}
  </script>
</body>
</html>
"""


def format_relative(ms, now_ms=None):
    if ms is None:
        return "unknown"
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    delta = max(0, now_ms - int(ms)) // 1000
    mins = delta // 60
    hours = mins // 60
    days = hours // 24

    parts = []
    if days:
        parts.append(f"{days} day" + ("s" if days != 1 else ""))
    if hours % 24:
        parts.append(f"{hours % 24} hour" + ("s" if hours % 24 != 1 else ""))
    if mins % 60 and not days:
        parts.append(f"{mins % 60} minute" + ("s" if mins % 60 != 1 else ""))
    if not parts:
        parts.append("just now")
    return " ".join(parts) + (" ago" if parts[0] != "just now" else "")


def time_colors(ms, now_ms=None):
    if ms is None:
        return "#333333", "#f2f2f2"
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    delta_hours = max(0, now_ms - int(ms)) / 3600000

    stops = [
        (1.0, (27, 94, 32)),
        (2.0, (133, 100, 18)),
        (3.0, (121, 69, 0)),
        (4.0, (122, 28, 28)),
    ]

    if delta_hours <= 1.0:
        rgb = stops[0][1]
    elif delta_hours >= 4.0:
        rgb = stops[-1][1]
    else:
        rgb = stops[-1][1]
        for i in range(len(stops) - 1):
            h0, c0 = stops[i]
            h1, c1 = stops[i + 1]
            if delta_hours <= h1:
                t = (delta_hours - h0) / (h1 - h0)
                rgb = (
                    int(round(c0[0] + (c1[0] - c0[0]) * t)),
                    int(round(c0[1] + (c1[1] - c0[1]) * t)),
                    int(round(c0[2] + (c1[2] - c0[2]) * t)),
                )
                break

    bg = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
    return bg, "#ffffff"


def latest_by_group(events, group_key):
    latest = {}
    for ev in events:
        if ev.get("trackGroupKey") != group_key:
            continue
        child_key = ev.get("childKey") or "unknown"
        current = latest.get(child_key)
        if not current or ev.get("beginDt", 0) > current.get("beginDt", 0):
            latest[child_key] = ev
    return latest


def latest_poopy_diapers(events):
    latest = {}
    for ev in events:
        if ev.get("trackGroupKey") != "DIAPER":
            continue
        payload = ev.get("payload") or {}
        if not payload.get("diaperTypePoop"):
            continue
        child_key = ev.get("childKey") or "unknown"
        current = latest.get(child_key)
        if not current or ev.get("beginDt", 0) > current.get("beginDt", 0):
            latest[child_key] = ev
    return latest


def local_midnight_ms(now_ms=None):
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    now_sec = now_ms / 1000.0
    local = time.localtime(now_sec)
    midnight_sec = time.mktime(
        (local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)
    )
    return int(midnight_sec * 1000)


def routine_counts_today(events, keywords, now_ms=None):
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    midnight_ms = local_midnight_ms(now_ms)
    normalized_keywords = [str(keyword).lower() for keyword in keywords]
    result = {}
    for ev in events:
        if ev.get("trackGroupKey") != "ROUTINE":
            continue
        payload = ev.get("payload") or {}
        name = payload.get("routineName") or ""
        routine_name = str(name).lower()
        if not any(keyword in routine_name for keyword in normalized_keywords):
            continue
        begin = ev.get("beginDt")
        if begin is None or int(begin) < midnight_ms:
            continue
        child_key = ev.get("childKey")
        if not child_key:
            continue
        result[child_key] = result.get(child_key, 0) + 1
    return result


def feed_label(ev):
    t = ev.get("trackTypeKey") or "FEED"
    payload = ev.get("payload") or {}
    if t == "FEED.BOTTLE":
        vol, unit = bottle_volume(payload)
        if vol is not None and unit:
            return f"Bottle ({format_amount(vol)} {display_volume_unit(unit)})"
        return "Bottle"
    if t == "FEED.BREAST":
        left = payload.get("breastLeftDuration")
        right = payload.get("breastRightDuration")
        secs = 0
        if isinstance(left, int):
            secs += left // 1000
        if isinstance(right, int):
            secs += right // 1000
        if secs:
            return f"Breast ({secs // 60} min)"
        return "Breast"
    if t == "FEED.SOLID":
        return "Solid"
    if t == "FEED.COMBO":
        return "Combo"
    return t


def format_amount(value):
    if value is None:
        return None
    rounded = round(value, 1)
    if abs(rounded - round(rounded)) < 1e-6:
        return str(int(round(rounded)))
    text = f"{rounded:.1f}"
    return text.rstrip("0").rstrip(".")


def to_number(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except ValueError:
        return None


def display_volume_unit(unit):
    normalized = str(unit or "mL").strip().lower()
    if normalized in {"ml", "milliliter", "milliliters"}:
        return "mL"
    if normalized in {"l", "liter", "liters"}:
        return "L"
    if normalized in {"oz", "fl oz", "floz", "fl_oz"}:
        return "oz"
    return str(unit or "").strip()


def bottle_volume(payload):
    unit = payload.get("bottleVolumeUnit") or payload.get("bottleFormulaVolumeUnit") or payload.get(
        "bottleBreastMilkVolumeUnit"
    )

    #num = to_number(payload.get("bottleVolumeNum"))
    #exp = to_number(payload.get("bottleVolumeExp"))
    #if num is not None and exp is not None:
    #    return num * (10 ** (-exp)), unit

    total = 0.0
    have = False
    for prefix in ("bottleFormulaVolume", "bottleBreastMilkVolume"):
        n = to_number(payload.get(f"{prefix}Num"))
        e = to_number(payload.get(f"{prefix}Exp"))
        if n is None or e is None:
            continue
        total += n * (10 ** (-e))
        have = True

    if have:
        return total, unit
    return None, unit


def bottle_milk_components(payload):
    unit = payload.get("bottleVolumeUnit") or payload.get("bottleFormulaVolumeUnit") or payload.get(
        "bottleBreastMilkVolumeUnit"
    )
    breast = None
    formula = None
    for prefix, key in (("bottleBreastMilkVolume", "breast"), ("bottleFormulaVolume", "formula")):
        n = to_number(payload.get(f"{prefix}Num"))
        e = to_number(payload.get(f"{prefix}Exp"))
        if n is None or e is None:
            continue
        value = n * (10 ** (-e))
        if key == "breast":
            breast = value
        else:
            formula = value
    return breast, formula, unit


def diaper_label(ev):
    if not ev:
        return "unknown"
    payload = ev.get("payload") or {}
    parts = []
    if payload.get("diaperTypePee"):
        parts.append("Wet")
    if payload.get("diaperTypePoop"):
        parts.append("Dirty")
    if payload.get("diaperTypeDry"):
        parts.append("Dry")
    if payload.get("diaperTypeRash"):
        parts.append("Rash")

    detail = payload.get("diaperDetail")
    color = payload.get("diaperDirtyColor")
    texture = payload.get("diaperDirtyTexture")
    extras = [v for v in (detail, color, texture) if isinstance(v, str) and v.strip()]
    if extras:
        parts.append(f"({', '.join(extras)})")

    return "/".join(parts) if parts else "Diaper"


def _empty_diaper_plot_stat():
    return {"count": 0, "blowoutCount": 0, "blowoutDetails": []}


def _new_diaper_plot_stats():
    return {mode: _empty_diaper_plot_stat() for mode in DIAPER_PLOT_MODES}


def diaper_plot_modes_for_event(ev):
    payload = ev.get("payload") or {}
    modes = ["all"]
    if payload.get("diaperTypePoop"):
        modes.append("dirty")
    if payload.get("diaperTypePee"):
        modes.append("wet")
    if payload.get("diaperTypeDry"):
        modes.append("dry")
    return modes


def diaper_blowout_details(ev):
    payload = ev.get("payload") or {}
    details = []
    for value in (ev.get("note"), payload.get("note"), payload.get("diaperDetail")):
        if not isinstance(value, str):
            continue
        text = value.strip()
        if text and text not in details:
            details.append(text)
    return details


def diaper_has_blowout(ev):
    payload = ev.get("payload") or {}
    if payload.get("diaperPoopBlowout"):
        return True
    return any("blowout" in detail.lower() for detail in diaper_blowout_details(ev))


def accumulate_diaper_plot_stats(mode_stats, ev):
    blowout = diaper_has_blowout(ev)
    blowout_details = diaper_blowout_details(ev) if blowout else []
    for mode in diaper_plot_modes_for_event(ev):
        stat = mode_stats.setdefault(mode, _empty_diaper_plot_stat())
        stat["count"] = int(stat.get("count", 0)) + 1
        if not blowout:
            continue
        stat["blowoutCount"] = int(stat.get("blowoutCount", 0)) + 1
        existing_details = stat.setdefault("blowoutDetails", [])
        for detail in blowout_details:
            if detail not in existing_details:
                existing_details.append(detail)


def combine_diaper_plot_stats(stats_list):
    combined = {mode: _empty_diaper_plot_stat() for mode in DIAPER_PLOT_MODES}
    for stats in stats_list:
        if not stats:
            continue
        for mode in DIAPER_PLOT_MODES:
            source = stats.get(mode)
            if not source:
                continue
            target = combined[mode]
            target["count"] += int(source.get("count", 0))
            target["blowoutCount"] += int(source.get("blowoutCount", 0))
            for detail in source.get("blowoutDetails", []):
                if detail not in target["blowoutDetails"]:
                    target["blowoutDetails"].append(detail)
    return combined


def diaper_plot_meta(stat):
    if not stat:
        return {"count": 0, "blowoutCount": 0, "blowoutDetails": []}
    return {
        "count": int(stat.get("count", 0)),
        "blowoutCount": int(stat.get("blowoutCount", 0)),
        "blowoutDetails": list(stat.get("blowoutDetails", [])),
    }


def format_days_count(delta_ms):
    rounded_tenths = max(0, int(round((float(delta_ms) / 86400000.0) * 10)))
    whole_days, tenths = divmod(rounded_tenths, 10)
    if tenths == 0:
        value = str(whole_days)
    else:
        value = f"{rounded_tenths / 10.0:.1f}".rstrip("0").rstrip(".")
    unit = "day" if rounded_tenths == 10 else "days"
    return f"{value} {unit}"


def poop_alert_text(name, last_poopy_diaper_ms, now_ms=None):
    if last_poopy_diaper_ms is None:
        return None
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    delta_ms = max(0, now_ms - int(last_poopy_diaper_ms))
    if delta_ms < POOP_ALERT_THRESHOLD_MS:
        return None
    return f"{name} hasn't pooped for {format_days_count(delta_ms)}."


def build_body(
    latest_feed,
    latest_diaper,
    latest_poopy_diapers_map,
    child_map,
    generated_at,
    vitamins=None,
    medications=None,
    baths=None,
):
    now_ms = int(time.time() * 1000)
    if vitamins is None:
        vitamins = {}
    if medications is None:
        medications = {}
    if baths is None:
        baths = {}
    rows = []
    alerts = []
    child_keys = sorted(
        ## Skip babies with no latest feed (dogs):
        latest_feed.keys(),
        ## All babies:
        #set(latest_feed.keys()) | set(latest_diaper.keys()),
        key=lambda key: (child_map.get(key) or key),
    )
    for child_key in child_keys:
        name = child_map.get(child_key) or child_key
        name_html = html.escape(name)
        poopy_diaper_ev = latest_poopy_diapers_map.get(child_key)
        alert_text = poop_alert_text(name, poopy_diaper_ev.get("beginDt") if poopy_diaper_ev else None, now_ms)
        vitamin_count = int(vitamins.get(child_key, 0) or 0)
        medication_count = int(medications.get(child_key, 0) or 0)
        bath_count = int(baths.get(child_key, 0) or 0)
        indicators = (
            (ALERT_ICON_HTML if alert_text else "")
            + ("&#128138;" * vitamin_count)
            + ("&#128137;" * medication_count)
            + ("&#128705;" * bath_count)
        )
        if indicators:
            name_html += f" {indicators}"
        feed_ev = latest_feed.get(child_key)
        diaper_ev = latest_diaper.get(child_key)
        feed_when = format_relative(feed_ev.get("beginDt"), now_ms) if feed_ev else "unknown"
        feed_text = feed_label(feed_ev) if feed_ev else "unknown"
        diaper_when = format_relative(diaper_ev.get("beginDt"), now_ms) if diaper_ev else "unknown"
        diaper_text = diaper_label(diaper_ev)
        feed_bg, feed_fg = time_colors(feed_ev.get("beginDt") if feed_ev else None, now_ms)
        diaper_bg, diaper_fg = time_colors(diaper_ev.get("beginDt") if diaper_ev else None, now_ms)
        rows.append(
            "<tr>"
            f"<td class=\"group\">{name_html}</td>"
            f"<td class=\"group\">{html.escape(feed_text)}</td>"
            f"<td class=\"time\" style=\"background:{feed_bg}; color:{feed_fg};\">{html.escape(feed_when)}</td>"
            f"<td class=\"group\">{html.escape(diaper_text)}</td>"
            f"<td class=\"time\" style=\"background:{diaper_bg}; color:{diaper_fg};\">{html.escape(diaper_when)}</td>"
            "</tr>"
        )
        if alert_text:
            alerts.append(f"{ALERT_ICON_HTML} {html.escape(alert_text)}")

    generated = time.strftime("%Y-%m-%d %H:%M", time.localtime(generated_at / 1000))
    rows_html = "\n".join(rows) or "<tr><td colspan=\"5\">No feeds found</td></tr>"
    alerts_html = ""
    if alerts:
        alert_lines = "\n".join(f'<div class="alert-line">{alert}</div>' for alert in alerts)
        alerts_html = f'<div class="alerts">{alert_lines}</div>'
    return f"""
    <table>
      <colgroup>
        <col class=\"col-baby\" />
        <col class=\"col-feed-type\" />
        <col class=\"col-feed-time\" />
        <col class=\"col-diaper-type\" />
        <col class=\"col-diaper-time\" />
      </colgroup>
      <thead>
        <tr>
          <th class=\"group\">Baby</th>
          <th class=\"group\" colspan=\"2\">Latest Feed</th>
          <th class=\"group\" colspan=\"2\">Latest Diaper</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
    {alerts_html}
    <div class=\"actions\">
      <div class=\"action-buttons\">
        <button class=\"btn\" onclick=\"openCleanWindow()\">Open Window</button>
        <a class=\"btn\" href=\"/plot\">Plots</a>
      </div>
      <div class=\"meta\">as of {html.escape(generated)}</div>
    </div>
    """.strip()


def build_html(
    latest_feed,
    latest_diaper,
    latest_poopy_diapers_map,
    child_map,
    generated_at,
    body_class="",
    vitamins=None,
    medications=None,
    baths=None,
):
    body_html = build_body(
        latest_feed,
        latest_diaper,
        latest_poopy_diapers_map,
        child_map,
        generated_at,
        vitamins,
        medications,
        baths,
    )
    css = (GLOBAL_CSS + """
    @view-transition { navigation: auto; }
    body {
      display: flex;
      justify-content: center;
      align-items: center;
    }
    body.bottom {
      align-items: flex-end;
    }
    .container {
      width: min(98vw, 1600px);
      padding: clamp(8px, 1.6vw, 24px);
    }
    .meta {
      color: #a3a3a3;
      font-size: clamp(12px, 1vw + 6px, 16px);
      white-space: nowrap;
    }
    @keyframes stale-age-pulse {
      0%, 100% { color: #ffd08a; text-shadow: none; transform: rotate(0deg); }
      50% { color: #ff9f1c; text-shadow: 0 0 12px rgba(255, 159, 28, 0.45); transform: rotate(-1.5deg); }
    }
    .meta-age {
      color: #ffd08a;
      display: inline-block;
      font-size: 1.35em;
      font-weight: 700;
      animation: stale-age-pulse 1.8s ease-in-out infinite;
    }
    .actions {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: clamp(8px, 1.2vw, 16px);
    }
    .alerts {
      margin-top: clamp(10px, 1.4vw, 18px);
      display: grid;
      gap: clamp(6px, 0.9vw, 12px);
    }
    .alert-line {
      color: #ffd08a;
      font-size: clamp(16px, 1.2vw + 10px, 24px);
      line-height: 1.35;
    }
    .action-buttons {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .btn {
      appearance: none;
      border: 1px solid #2a2a2a;
      background: #141414;
      color: #f2f2f2;
      padding: 8px 12px;
      font-family: var(--font-body);
      font-size: clamp(12px, 1vw + 6px, 16px);
      border-radius: 6px;
      cursor: pointer;
      text-decoration: none;
      display: inline-block;
    }
    .btn:hover { background: #1b1b1b; }
    table {
      border-collapse: collapse;
      width: 100%;
      font-size: clamp(12px, 1.4vw + 8px, 30px);
      table-layout: fixed;
    }
    th, td {
      text-align: left;
      padding: clamp(8px, 1.2vw, 16px) clamp(10px, 1.6vw, 22px);
      border-bottom: 1px solid #2a2a2a;
      line-height: 1.2;
    }
    th {
      background: #333;
      text-align: center;
      font-family: var(--font-display);
      font-weight: 400;
      font-size: clamp(14px, 1.8vw + 8px, 36px);
    }
    th.group, td.group { border-left: 2px solid #222222; }
    th.time, td.time { text-align: right; }
    .col-baby { width: 17%; }
    .col-feed-type { width: 19%; }
    .col-feed-time { width: 26%; }
    .col-diaper-type { width: 12%; }
    .col-diaper-time { width: 26%; }
    """).strip()
    script = """
    const REFRESH_INTERVAL_MS = 60000;
    let lastSuccessMs = Date.now();
    let staleActive = false;
    let refreshPromise = null;

    function openCleanWindow() {
      const features = "toolbar=no,location=no,menubar=no,scrollbars=yes,resizable=yes";
      window.open(window.location.href, "nara_clean", features);
    }

    function updateStaleNote() {
      const meta = document.querySelector(".meta");
      if (!meta) {
        return;
      }
      if (!meta.dataset.base) {
        meta.dataset.base = meta.textContent || "";
      }
      const baseText = meta.dataset.base;

      function renderMeta(suffix = "") {
        if (!suffix) {
          meta.textContent = baseText;
          return;
        }
        const age = document.createElement("span");
        age.className = "meta-age";
        age.textContent = `(${suffix})`;
        meta.replaceChildren(
          document.createTextNode(`${baseText} `),
          age,
        );
      }

      if (!staleActive) {
        renderMeta();
        return;
      }
      const minutes = Math.max(0, Math.floor((Date.now() - lastSuccessMs) / 60000));
      if (minutes === 0) {
        renderMeta();
        return;
      }
      const suffix = minutes === 1 ? "1 min old" : `${minutes} mins old`;
      renderMeta(suffix);
    }

    async function refreshContent() {
      if (refreshPromise) {
        return refreshPromise;
      }
      refreshPromise = (async () => {
      try {
        const url = new URL(window.location.href);
        url.searchParams.set("_", Date.now().toString());
        const response = await fetch(url, { cache: "no-store" });
        if (response.status === 401) {
          window.location.reload();
          return;
        }
        if (!response.ok) {
          staleActive = true;
          updateStaleNote();
          console.warn("Refresh failed", response.status);
          return;
        }
        const htmlText = await response.text();
        const parsed = new DOMParser().parseFromString(htmlText, "text/html");
        const nextContainer = parsed.querySelector(".container");
        const container = document.querySelector(".container");
        if (container && nextContainer) {
          container.innerHTML = nextContainer.innerHTML;
          lastSuccessMs = Date.now();
          staleActive = false;
          updateStaleNote();
        } else {
          staleActive = true;
          updateStaleNote();
          console.warn("Refresh failed: missing container");
        }
      } catch (err) {
        staleActive = true;
        updateStaleNote();
        console.warn("Refresh error", err);
      } finally {
        refreshPromise = null;
      }
      })();
      return refreshPromise;
    }

    function refreshIfStale() {
      if ((Date.now() - lastSuccessMs) < REFRESH_INTERVAL_MS) {
        return;
      }
      refreshContent();
    }

    updateStaleNote();
    window.addEventListener("pageshow", refreshIfStale);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        refreshIfStale();
      }
    });

    setInterval(refreshContent, REFRESH_INTERVAL_MS);
    """.strip()
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Nara Feeds</title>
  <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
  <style>
    {css}
  </style>
</head>
<body class="{html.escape(body_class)}">
  <div class="container">
    {body_html}
  </div>
  <script>
    {script}
  </script>
</body>
</html>
"""




def build_json(
    latest_feed,
    latest_diaper,
    latest_poopy_diapers_map,
    child_map,
    generated_at,
    vitamins=None,
    medications=None,
    baths=None,
):
    if vitamins is None:
        vitamins = {}
    if medications is None:
        medications = {}
    if baths is None:
        baths = {}
    child_keys = sorted(
        latest_feed.keys(),
        key=lambda key: (child_map.get(key) or key),
    )
    children = []
    for child_key in child_keys:
        name = child_map.get(child_key) or child_key
        feed_ev = latest_feed.get(child_key)
        diaper_ev = latest_diaper.get(child_key)
        poopy_diaper_ev = latest_poopy_diapers_map.get(child_key)
        vitamin_count = int(vitamins.get(child_key, 0) or 0)
        medication_count = int(medications.get(child_key, 0) or 0)
        bath_count = int(baths.get(child_key, 0) or 0)
        children.append(
            {
                "id": child_key,
                "name": name,
                "vitaminsToday": vitamin_count,
                "medicationToday": medication_count,
                "bathsToday": bath_count,
                "feed": {
                    "label": feed_label(feed_ev) if feed_ev else "unknown",
                    "beginDt": feed_ev.get("beginDt") if feed_ev else None,
                },
                "diaper": {
                    "label": diaper_label(diaper_ev) if diaper_ev else "unknown",
                    "beginDt": diaper_ev.get("beginDt") if diaper_ev else None,
                },
                "lastPoopDiaperBeginDt": poopy_diaper_ev.get("beginDt") if poopy_diaper_ev else None,
            }
        )
    return {
        "generatedAt": generated_at,
        "children": children,
    }


def normalize_milk_to_ml(volume, unit):
    if volume is None:
        return None
    normalized = str(unit or "mL").strip().lower()
    if normalized in {"ml", "milliliter", "milliliters"}:
        return float(volume)
    if normalized in {"l", "liter", "liters"}:
        return float(volume) * 1000.0
    if normalized in {"oz", "fl oz", "floz", "fl_oz"}:
        return float(volume) * 29.5735
    return None


def _trim_milk_series(daily_points, cumulative_points):
    first_nonzero_idx = None
    last_nonzero_idx = None
    for idx, value in enumerate(daily_points):
        if value > 0:
            if first_nonzero_idx is None:
                first_nonzero_idx = idx
            last_nonzero_idx = idx

    if first_nonzero_idx is None:
        return None, None

    daily_display = []
    cumulative_display = []
    for idx, daily_value in enumerate(daily_points):
        cumulative_value = cumulative_points[idx]
        if idx < first_nonzero_idx or idx > cast(int, last_nonzero_idx):
            daily_display.append(None)
            if idx == first_nonzero_idx - 1:
                cumulative_display.append(0.0)
            else:
                cumulative_display.append(None)
            continue
        daily_display.append(round(daily_value, 1))
        cumulative_display.append(round(cumulative_value, 1))

    return daily_display, cumulative_display


def _trim_optional_series(values, decimals=1):
    first_idx = None
    last_idx = None
    for idx, value in enumerate(values):
        if value is not None:
            if first_idx is None:
                first_idx = idx
            last_idx = idx

    if first_idx is None or last_idx is None:
        return None

    output = []
    for idx, value in enumerate(values):
        if idx < first_idx or idx > last_idx or value is None:
            output.append(None)
            continue
        output.append(round(float(value), decimals))
    return output


def _mask_series_with_numeric(values, numeric_mask):
    if values is None or numeric_mask is None:
        return None
    output = []
    limit = min(len(values), len(numeric_mask))
    for idx in range(limit):
        output.append(values[idx] if numeric_mask[idx] is not None else None)
    return output


def _format_gap_period(start_ms, end_ms):
    if start_ms is None or end_ms is None:
        return None
    start_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(start_ms) / 1000.0))
    end_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(end_ms) / 1000.0))
    return f"{start_text} to {end_text}"


def _trim_count_series(values, decimals=0):
    first_nonzero_idx = None
    last_nonzero_idx = None
    for idx, value in enumerate(values):
        if float(value) > 0:
            if first_nonzero_idx is None:
                first_nonzero_idx = idx
            last_nonzero_idx = idx

    if first_nonzero_idx is None or last_nonzero_idx is None:
        return None

    output = []
    for idx, value in enumerate(values):
        if idx < first_nonzero_idx or idx > last_nonzero_idx:
            output.append(None)
            continue
        output.append(round(float(value), decimals))
    return output


def is_night_hour(hour, night_start_hour):
    night_end_hour = (night_start_hour + 12) % 24
    if night_start_hour < night_end_hour:
        return night_start_hour <= hour < night_end_hour
    return hour >= night_start_hour or hour < night_end_hour


def _rounded_plot_float(value, decimals=12):
    return round(float(value), decimals)


def _plot_gap_hourly_by_day(labels, gap_stats_by_day_hour):
    hourly = {}
    for day_key in labels:
        day_payload = {}
        for hour, stat in sorted((gap_stats_by_day_hour.get(day_key) or {}).items()):
            gap_count = int(stat.get("count", 0))
            if gap_count <= 0:
                continue
            day_payload[str(hour)] = {
                "gapSum": _rounded_plot_float(stat.get("sum", 0.0)),
                "gapCount": gap_count,
                "maxGap": _rounded_plot_float(stat.get("max", 0.0)),
                "maxGapDisplay": round(float(stat.get("max", 0.0)), 2),
                "maxGapPeriod": _format_gap_period(stat.get("maxStart"), stat.get("maxEnd")),
            }
        if day_payload:
            hourly[day_key] = day_payload
    return hourly


def _plot_diaper_hourly_payload(mode_stats):
    diaper_payload = {}
    for mode in DIAPER_PLOT_MODES:
        stat = (mode_stats or {}).get(mode)
        if not stat or int(stat.get("count", 0)) <= 0:
            continue
        diaper_payload[mode] = diaper_plot_meta(stat)
    return diaper_payload


def _plot_child_hourly_by_day(
    labels,
    day_hour_totals,
    breast_day_hour_totals,
    formula_day_hour_totals,
    day_hour_feed_counts,
    day_hour_max_feeds,
    diaper_day_hour_stats,
    child_gap_hour_stats,
):
    hourly = {}
    for day_key in labels:
        hour_keys = set()
        hour_sources = (
            day_hour_totals,
            breast_day_hour_totals,
            formula_day_hour_totals,
            day_hour_feed_counts,
            day_hour_max_feeds,
            diaper_day_hour_stats,
            child_gap_hour_stats,
        )
        for source in hour_sources:
            hour_keys.update((source.get(day_key) or {}).keys())

        day_payload = {}
        for hour in sorted(hour_keys):
            hour_payload = {}
            daily = (day_hour_totals.get(day_key) or {}).get(hour, 0.0)
            if daily:
                hour_payload["daily"] = _rounded_plot_float(daily)

            breast_daily = (breast_day_hour_totals.get(day_key) or {}).get(hour, 0.0)
            if breast_daily:
                hour_payload["breastDaily"] = _rounded_plot_float(breast_daily)

            formula_daily = (formula_day_hour_totals.get(day_key) or {}).get(hour, 0.0)
            if formula_daily:
                hour_payload["formulaDaily"] = _rounded_plot_float(formula_daily)

            feed_count = int((day_hour_feed_counts.get(day_key) or {}).get(hour, 0))
            if feed_count:
                hour_payload["feedCount"] = feed_count

            max_feed = (day_hour_max_feeds.get(day_key) or {}).get(hour)
            if max_feed is not None:
                hour_payload["maxMilkPerFeed"] = _rounded_plot_float(max_feed)

            diaper_payload = _plot_diaper_hourly_payload(
                (diaper_day_hour_stats.get(day_key) or {}).get(hour)
            )
            if diaper_payload:
                hour_payload["diaper"] = diaper_payload

            gap_stat = (child_gap_hour_stats.get(day_key) or {}).get(hour)
            if gap_stat and int(gap_stat.get("count", 0)) > 0:
                hour_payload["gapSum"] = _rounded_plot_float(gap_stat.get("sum", 0.0))
                hour_payload["gapCount"] = int(gap_stat.get("count", 0))
                hour_payload["maxGap"] = _rounded_plot_float(gap_stat.get("max", 0.0))
                hour_payload["maxGapDisplay"] = round(float(gap_stat.get("max", 0.0)), 2)
                hour_payload["maxGapPeriod"] = _format_gap_period(
                    gap_stat.get("maxStart"), gap_stat.get("maxEnd")
                )

            if hour_payload:
                day_payload[str(hour)] = hour_payload

        if day_payload:
            hourly[day_key] = day_payload
    return hourly


def _timeline_day_minute(timestamp_ms):
    local_time = time.localtime(int(timestamp_ms) / 1000.0)
    return time.strftime("%Y-%m-%d", local_time), local_time.tm_hour * 60 + local_time.tm_min


def _timeline_event_note(ev):
    payload = ev.get("payload") or {}
    for value in (ev.get("note"), payload.get("note")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _timeline_event_type(ev):
    group = ev.get("trackGroupKey") or "UNKNOWN"
    payload = ev.get("payload") or {}
    if group == "FEED":
        feed_type = str(payload.get("feedType") or "").strip().lower()
        label = "Nursing" if feed_type == "nursing" else "Bottle feed" if feed_type == "bottle" else "Feed"
        return "feed", "🍼", label
    if group == "DIAPER":
        if payload.get("diaperPoopBlowout") or diaper_has_blowout(ev):
            return "diaper-blowout", "💩", "Blowout"
        if payload.get("diaperTypePoop"):
            return "diaper-dirty", "💩", "Dirty diaper"
        if payload.get("diaperTypePee"):
            return "diaper-wet", "💧", "Wet diaper"
        if payload.get("diaperTypeDry"):
            return "diaper-dry", "🐪", "Dry diaper"
        return "diaper", "🧷", "Diaper"
    if group == "PUMP":
        return "pump", "🥛", "Pump"
    if group == "ROUTINE":
        name = str(payload.get("routineName") or "Routine").strip()
        lower_name = name.lower()
        if "bath" in lower_name:
            return "bath", "🛁", name
        if "vitamin" in lower_name or "probiotic" in lower_name:
            return "vitamin", "💊", name
        if "med" in lower_name:
            return "medication", "💉", name
        return "routine", "✅", name
    if group == "SLEEP":
        return "sleep", "💤", "Sleep"
    if group == "GROW":
        return "growth", "📏", "Growth"
    if group == "ALBUM":
        return "album", "🖼️", str(payload.get("albumName") or "Album")
    if group == "MEDICAL":
        return "medical", "🩺", "Medical"
    return group.lower(), "•", group.title()


def _timeline_event_value(ev):
    group = ev.get("trackGroupKey")
    payload = ev.get("payload") or {}
    if group == "FEED":
        volume, unit = bottle_volume(payload)
        volume_ml = normalize_milk_to_ml(volume, unit) if volume is not None else None
        return round(float(volume_ml), 1) if volume_ml is not None else None
    if group == "GROW":
        weight_num = to_number(payload.get("weightNum"))
        weight_exp = to_number(payload.get("weightExp"))
        if weight_num is not None and weight_exp is not None:
            return round(float(weight_num) * (10 ** (-int(weight_exp))), 2)
    return None


def _build_timeline_payload(events, child_map, palette):
    day_keys = set()
    timeline_events = []
    for ev in events:
        begin_dt = ev.get("beginDt")
        if begin_dt is None:
            continue
        begin_day, begin_minute = _timeline_day_minute(begin_dt)
        day_keys.add(begin_day)
        end_minute = None
        end_dt = ev.get("endDt")
        end_day = None
        if end_dt is not None:
            end_day, end_minute = _timeline_day_minute(end_dt)
            day_keys.add(end_day)
        timeline_events.append((ev, begin_day, begin_minute, end_day, end_minute))

    days = sorted(day_keys)
    day_index = {day: idx for idx, day in enumerate(days)}
    child_keys = sorted(
        {ev.get("childKey") for ev, *_ in timeline_events if ev.get("childKey")},
        key=lambda key: (child_map.get(key) or key),
    )
    children = [
        {
            "key": child_key,
            "label": child_map.get(child_key) or child_key,
            "borderColor": palette[idx % len(palette)],
            "backgroundColor": palette[idx % len(palette)],
        }
        for idx, child_key in enumerate(child_keys)
    ]
    general_child_index = len(children)
    children.append(
        {
            "key": "",
            "label": "General",
            "borderColor": "#ffffff",
            "backgroundColor": "#ffffff",
        }
    )
    child_index = {child["key"]: idx for idx, child in enumerate(children)}

    type_index = {}
    types = []
    note_index = {}
    notes = []
    points = []

    def get_type_index(key, emoji, label):
        if key not in type_index:
            type_index[key] = len(types)
            types.append({"key": key, "emoji": emoji, "label": label})
        return type_index[key]

    def get_note_index(note):
        if note is None:
            return None
        if note not in note_index:
            note_index[note] = len(notes)
            notes.append(note)
        return note_index[note]

    for ev, begin_day, begin_minute, end_day, end_minute in timeline_events:
        child_key = ev.get("childKey") or ""
        event_type_key, emoji, event_label = _timeline_event_type(ev)
        type_idx = get_type_index(event_type_key, emoji, event_label)
        child_idx = child_index.get(child_key, general_child_index)
        end_value = None
        if end_day is not None and end_minute is not None:
            end_value = end_minute
            if end_day != begin_day and begin_day in day_index and end_day in day_index:
                end_value += (day_index[end_day] - day_index[begin_day]) * 1440
        point = [
            day_index[begin_day],
            begin_minute,
            child_idx,
            type_idx,
            end_value,
            _timeline_event_value(ev),
            get_note_index(_timeline_event_note(ev)),
        ]
        while point and point[-1] is None:
            point.pop()
        points.append(point)

    return {
        "days": days,
        "children": children,
        "types": types,
        "notes": notes,
        "points": points,
    }


def milk_totals_by_day(events, child_map):
    by_child_day = {}
    by_child_day_hour = {}
    max_feed_by_child_day = {}
    max_feed_by_child_day_hour = {}
    breast_by_child_day = {}
    breast_by_child_day_hour = {}
    formula_by_child_day = {}
    formula_by_child_day_hour = {}
    feed_counts_by_child_day = {}
    feed_counts_by_child_day_hour = {}
    diaper_stats_by_child_day = {}
    diaper_stats_by_child_day_hour = {}
    feed_times_by_child = {}
    feed_times_all = []
    gap_stats_by_child_day = {}
    gap_stats_by_child_day_hour = {}
    gap_stats_all_day = {}
    gap_stats_all_day_hour = {}
    day_keys = set()
    skipped_units = 0
    for ev in events:
        track_group = ev.get("trackGroupKey")
        if track_group not in {"FEED", "DIAPER"}:
            continue
        child_key = ev.get("childKey")
        begin_dt = ev.get("beginDt")
        if not child_key or begin_dt is None:
            continue
        begin_dt = int(begin_dt)
        begin_local = time.localtime(begin_dt / 1000.0)
        day_key = time.strftime("%Y-%m-%d", begin_local)
        day_keys.add(day_key)
        hour = begin_local.tm_hour

        if track_group == "DIAPER":
            child_diaper_stats = diaper_stats_by_child_day.setdefault(child_key, {})
            day_diaper_stats = child_diaper_stats.setdefault(day_key, _new_diaper_plot_stats())
            accumulate_diaper_plot_stats(day_diaper_stats, ev)
            child_diaper_hour_stats = diaper_stats_by_child_day_hour.setdefault(child_key, {})
            day_hour_stats = child_diaper_hour_stats.setdefault(day_key, {})
            hour_diaper_stats = day_hour_stats.setdefault(hour, _new_diaper_plot_stats())
            accumulate_diaper_plot_stats(hour_diaper_stats, ev)
            continue

        child_feed_times = feed_times_by_child.setdefault(child_key, [])
        child_feed_times.append(begin_dt)
        feed_times_all.append(begin_dt)

        payload = ev.get("payload") or {}
        volume, unit = bottle_volume(payload)
        if volume is None:
            continue
        volume_ml = normalize_milk_to_ml(volume, unit)
        if volume_ml is None:
            skipped_units += 1
            continue
        child_days = by_child_day.setdefault(child_key, {})
        child_days[day_key] = child_days.get(day_key, 0.0) + volume_ml
        child_day_hours = by_child_day_hour.setdefault(child_key, {})
        day_hours = child_day_hours.setdefault(day_key, {})
        day_hours[hour] = day_hours.get(hour, 0.0) + volume_ml
        child_day_max_feeds = max_feed_by_child_day.setdefault(child_key, {})
        current_day_max = child_day_max_feeds.get(day_key)
        if current_day_max is None or volume_ml > current_day_max:
            child_day_max_feeds[day_key] = volume_ml
        child_day_hour_max_feeds = max_feed_by_child_day_hour.setdefault(child_key, {})
        day_hour_max_feeds = child_day_hour_max_feeds.setdefault(day_key, {})
        current_hour_max = day_hour_max_feeds.get(hour)
        if current_hour_max is None or volume_ml > current_hour_max:
            day_hour_max_feeds[hour] = volume_ml

        breast_volume, formula_volume, component_unit = bottle_milk_components(payload)
        if breast_volume is not None or formula_volume is not None:
            breast_ml = normalize_milk_to_ml(breast_volume, component_unit) if breast_volume is not None else 0.0
            formula_ml = normalize_milk_to_ml(formula_volume, component_unit) if formula_volume is not None else 0.0
            if breast_ml is None or formula_ml is None:
                skipped_units += 1
            else:
                child_breast_days = breast_by_child_day.setdefault(child_key, {})
                child_breast_days[day_key] = child_breast_days.get(day_key, 0.0) + breast_ml
                child_breast_day_hours = breast_by_child_day_hour.setdefault(child_key, {})
                breast_day_hours = child_breast_day_hours.setdefault(day_key, {})
                breast_day_hours[hour] = breast_day_hours.get(hour, 0.0) + breast_ml

                child_formula_days = formula_by_child_day.setdefault(child_key, {})
                child_formula_days[day_key] = child_formula_days.get(day_key, 0.0) + formula_ml
                child_formula_day_hours = formula_by_child_day_hour.setdefault(child_key, {})
                formula_day_hours = child_formula_day_hours.setdefault(day_key, {})
                formula_day_hours[hour] = formula_day_hours.get(hour, 0.0) + formula_ml
        child_day_counts = feed_counts_by_child_day.setdefault(child_key, {})
        child_day_counts[day_key] = child_day_counts.get(day_key, 0) + 1
        child_day_hour_counts = feed_counts_by_child_day_hour.setdefault(child_key, {})
        day_hour_counts = child_day_hour_counts.setdefault(day_key, {})
        day_hour_counts[hour] = day_hour_counts.get(hour, 0) + 1

    for child_key, feed_times in feed_times_by_child.items():
        if len(feed_times) < 2:
            continue
        feed_times.sort()
        prev_dt = feed_times[0]
        for current_dt in feed_times[1:]:
            if current_dt <= prev_dt:
                prev_dt = current_dt
                continue
            gap_hours = (current_dt - prev_dt) / 3600000.0
            gap_day_key = time.strftime("%Y-%m-%d", time.localtime(current_dt / 1000.0))
            gap_hour = time.localtime(current_dt / 1000.0).tm_hour
            child_gap_stats = gap_stats_by_child_day.setdefault(child_key, {})
            stat = child_gap_stats.setdefault(gap_day_key, {"sum": 0.0, "count": 0, "max": 0.0})
            stat["sum"] += gap_hours
            stat["count"] += 1
            if gap_hours > float(stat["max"]):
                stat["max"] = gap_hours
                stat["maxStart"] = prev_dt
                stat["maxEnd"] = current_dt

            child_gap_hour_stats = gap_stats_by_child_day_hour.setdefault(child_key, {})
            day_hour_stats = child_gap_hour_stats.setdefault(gap_day_key, {})
            hour_stat = day_hour_stats.setdefault(gap_hour, {"sum": 0.0, "count": 0, "max": 0.0})
            hour_stat["sum"] += gap_hours
            hour_stat["count"] += 1
            if gap_hours > float(hour_stat["max"]):
                hour_stat["max"] = gap_hours
                hour_stat["maxStart"] = prev_dt
                hour_stat["maxEnd"] = current_dt
            prev_dt = current_dt

    if len(feed_times_all) >= 2:
        feed_times_all.sort()
        prev_dt = feed_times_all[0]
        for current_dt in feed_times_all[1:]:
            if current_dt <= prev_dt:
                prev_dt = current_dt
                continue
            gap_hours = (current_dt - prev_dt) / 3600000.0
            gap_day_key = time.strftime("%Y-%m-%d", time.localtime(current_dt / 1000.0))
            gap_hour = time.localtime(current_dt / 1000.0).tm_hour
            stat = gap_stats_all_day.setdefault(gap_day_key, {"sum": 0.0, "count": 0, "max": 0.0})
            stat["sum"] += gap_hours
            stat["count"] += 1
            if gap_hours > float(stat["max"]):
                stat["max"] = gap_hours
                stat["maxStart"] = prev_dt
                stat["maxEnd"] = current_dt

            day_hour_stats = gap_stats_all_day_hour.setdefault(gap_day_key, {})
            hour_stat = day_hour_stats.setdefault(gap_hour, {"sum": 0.0, "count": 0, "max": 0.0})
            hour_stat["sum"] += gap_hours
            hour_stat["count"] += 1
            if gap_hours > float(hour_stat["max"]):
                hour_stat["max"] = gap_hours
                hour_stat["maxStart"] = prev_dt
                hour_stat["maxEnd"] = current_dt
            prev_dt = current_dt

    if not day_keys:
        return {
            "labels": [],
            "series": [],
            "allBabiesGap": None,
            "timeline": _build_timeline_payload(events, child_map, [
                "#d93025",
                "#1e88e5",
                "#0f9d58",
                "#f9ab00",
                "#8e24aa",
                "#00897b",
                "#6d4c41",
                "#5e35b1",
            ]),
            "defaultNightStart": 20,
            "skippedUnits": skipped_units,
        }

    start_day = date.fromisoformat(min(day_keys))
    end_day = date.fromisoformat(max(day_keys))
    labels = []
    cursor = start_day
    while cursor <= end_day:
        labels.append(cursor.isoformat())
        cursor += timedelta(days=1)

    palette = [
        "#d93025",
        "#1e88e5",
        "#0f9d58",
        "#f9ab00",
        "#8e24aa",
        "#00897b",
        "#6d4c41",
        "#5e35b1",
    ]

    all_babies_max_gap_points = []
    all_babies_max_gap_periods = []
    all_babies_avg_gap_points = []
    for day_key in labels:
        gap_stat = gap_stats_all_day.get(day_key)
        if gap_stat and gap_stat.get("count", 0) > 0:
            all_babies_max_gap_points.append(float(gap_stat.get("max", 0.0)))
            all_babies_max_gap_periods.append(
                _format_gap_period(gap_stat.get("maxStart"), gap_stat.get("maxEnd"))
            )
            all_babies_avg_gap_points.append(
                float(gap_stat.get("sum", 0.0)) / float(gap_stat.get("count", 1))
            )
        else:
            all_babies_max_gap_points.append(None)
            all_babies_max_gap_periods.append(None)
            all_babies_avg_gap_points.append(None)

    all_babies_max_gap_display = _trim_optional_series(all_babies_max_gap_points, decimals=2)
    all_babies_avg_gap_display = _trim_optional_series(all_babies_avg_gap_points, decimals=2)
    all_babies_max_gap_period_display = _mask_series_with_numeric(
        all_babies_max_gap_periods, all_babies_max_gap_display
    )
    if all_babies_max_gap_display is None:
        all_babies_max_gap_display = [None] * len(labels)
    if all_babies_max_gap_period_display is None:
        all_babies_max_gap_period_display = [None] * len(labels)
    if all_babies_avg_gap_display is None:
        all_babies_avg_gap_display = [None] * len(labels)

    all_babies_gap_hourly = _plot_gap_hourly_by_day(labels, gap_stats_all_day_hour)

    series = []
    series_child_keys = sorted(
        set(by_child_day.keys())
        | set(diaper_stats_by_child_day.keys())
        | set(gap_stats_by_child_day.keys()),
        key=lambda key: (child_map.get(key) or key),
    )
    for idx, child_key in enumerate(series_child_keys):
        day_totals = by_child_day.get(child_key, {})
        day_hour_totals = by_child_day_hour.get(child_key, {})
        breast_day_totals = breast_by_child_day.get(child_key, {})
        breast_day_hour_totals = breast_by_child_day_hour.get(child_key, {})
        formula_day_totals = formula_by_child_day.get(child_key, {})
        formula_day_hour_totals = formula_by_child_day_hour.get(child_key, {})
        day_feed_counts = feed_counts_by_child_day.get(child_key, {})
        day_hour_feed_counts = feed_counts_by_child_day_hour.get(child_key, {})
        day_max_feeds = max_feed_by_child_day.get(child_key, {})
        day_hour_max_feeds = max_feed_by_child_day_hour.get(child_key, {})
        diaper_day_stats = diaper_stats_by_child_day.get(child_key, {})
        diaper_day_hour_stats = diaper_stats_by_child_day_hour.get(child_key, {})
        child_gap_stats = gap_stats_by_child_day.get(child_key, {})
        child_gap_hour_stats = gap_stats_by_child_day_hour.get(child_key, {})

        running_total = 0.0
        daily_points = []
        breast_daily_points = []
        formula_daily_points = []
        cumulative_points = []
        avg_milk_per_feed_points = []
        max_milk_per_feed_points = []
        breast_milk_percent_points = []
        diaper_points_by_mode = {mode: [] for mode in DIAPER_PLOT_MODES}
        diaper_meta_by_mode = {mode: [] for mode in DIAPER_PLOT_MODES}
        max_gap_points = []
        max_gap_periods = []
        avg_gap_points = []
        for day_key in labels:
            daily_value = day_totals.get(day_key, 0.0)
            daily_feed_count = int(day_feed_counts.get(day_key, 0))
            breast_value = breast_day_totals.get(day_key, 0.0)
            formula_value = formula_day_totals.get(day_key, 0.0)
            mix_total = breast_value + formula_value
            running_total += daily_value
            daily_points.append(daily_value)
            breast_daily_points.append(breast_value)
            formula_daily_points.append(formula_value)
            cumulative_points.append(running_total)
            avg_milk_per_feed_points.append(
                daily_value / daily_feed_count if daily_feed_count > 0 else None
            )
            max_milk_per_feed_points.append(day_max_feeds.get(day_key))
            breast_milk_percent_points.append((breast_value * 100.0 / mix_total) if mix_total > 0 else None)
            day_diaper_mode_stats = diaper_day_stats.get(day_key, {})
            for mode in DIAPER_PLOT_MODES:
                stat = day_diaper_mode_stats.get(mode)
                diaper_points_by_mode[mode].append(int(stat.get("count", 0)) if stat else 0)
                diaper_meta_by_mode[mode].append(diaper_plot_meta(stat))

            gap_stat = child_gap_stats.get(day_key)
            if gap_stat and gap_stat.get("count", 0) > 0:
                max_gap_points.append(float(gap_stat.get("max", 0.0)))
                max_gap_periods.append(
                    _format_gap_period(gap_stat.get("maxStart"), gap_stat.get("maxEnd"))
                )
                avg_gap_points.append(float(gap_stat.get("sum", 0.0)) / float(gap_stat.get("count", 1)))
            else:
                max_gap_points.append(None)
                max_gap_periods.append(None)
                avg_gap_points.append(None)

        daily_display, cumulative_display = _trim_milk_series(daily_points, cumulative_points)
        if daily_display is None or cumulative_display is None:
            daily_display = [None] * len(labels)
            cumulative_display = [None] * len(labels)
        breast_daily_display = _trim_count_series(breast_daily_points, decimals=1)
        formula_daily_display = _trim_count_series(formula_daily_points, decimals=1)
        if breast_daily_display is None:
            breast_daily_display = [None] * len(labels)
        if formula_daily_display is None:
            formula_daily_display = [None] * len(labels)

        max_gap_display = _trim_optional_series(max_gap_points, decimals=2)
        max_gap_period_display = _mask_series_with_numeric(max_gap_periods, max_gap_display)
        avg_gap_display = _trim_optional_series(avg_gap_points, decimals=2)
        avg_milk_per_feed_display = _trim_optional_series(avg_milk_per_feed_points, decimals=1)
        max_milk_per_feed_display = _trim_optional_series(max_milk_per_feed_points, decimals=1)
        breast_milk_percent_display = _trim_optional_series(breast_milk_percent_points, decimals=1)
        diaper_display_by_mode = {}
        diaper_meta_display_by_mode = {}
        for mode in DIAPER_PLOT_MODES:
            display = _trim_count_series(diaper_points_by_mode[mode])
            if display is None:
                display = [None] * len(labels)
            diaper_display_by_mode[mode] = display
            meta_display = _mask_series_with_numeric(diaper_meta_by_mode[mode], display)
            if meta_display is None:
                meta_display = [None] * len(labels)
            diaper_meta_display_by_mode[mode] = meta_display
        if max_gap_display is None:
            max_gap_display = [None] * len(labels)
        if max_gap_period_display is None:
            max_gap_period_display = [None] * len(labels)
        if avg_gap_display is None:
            avg_gap_display = [None] * len(labels)
        if avg_milk_per_feed_display is None:
            avg_milk_per_feed_display = [None] * len(labels)
        if max_milk_per_feed_display is None:
            max_milk_per_feed_display = [None] * len(labels)
        if breast_milk_percent_display is None:
            breast_milk_percent_display = [None] * len(labels)
        hourly = _plot_child_hourly_by_day(
            labels,
            day_hour_totals,
            breast_day_hour_totals,
            formula_day_hour_totals,
            day_hour_feed_counts,
            day_hour_max_feeds,
            diaper_day_hour_stats,
            child_gap_hour_stats,
        )

        series.append(
            {
                "label": child_map.get(child_key) or child_key,
                "daily": daily_display,
                "breastDaily": breast_daily_display,
                "formulaDaily": formula_daily_display,
                "cumulative": cumulative_display,
                "avgMilkPerFeed": avg_milk_per_feed_display,
                "maxMilkPerFeed": max_milk_per_feed_display,
                "breastMilkPercent": breast_milk_percent_display,
                "diaperAll": diaper_display_by_mode["all"],
                "diaperAllMeta": diaper_meta_display_by_mode["all"],
                "diaperDirty": diaper_display_by_mode["dirty"],
                "diaperDirtyMeta": diaper_meta_display_by_mode["dirty"],
                "diaperWet": diaper_display_by_mode["wet"],
                "diaperWetMeta": diaper_meta_display_by_mode["wet"],
                "diaperDry": diaper_display_by_mode["dry"],
                "diaperDryMeta": diaper_meta_display_by_mode["dry"],
                "maxGap": max_gap_display,
                "maxGapPeriod": max_gap_period_display,
                "avgGap": avg_gap_display,
                "hourly": hourly,
                "borderColor": palette[idx % len(palette)],
                "backgroundColor": palette[idx % len(palette)],
            }
        )

    return {
        "labels": labels,
        "series": series,
        "allBabiesGap": {
            "label": "All Babies",
            "maxGap": all_babies_max_gap_display,
            "maxGapPeriod": all_babies_max_gap_period_display,
            "avgGap": all_babies_avg_gap_display,
            "hourly": all_babies_gap_hourly,
        },
        "timeline": _build_timeline_payload(events, child_map, palette),
        "defaultNightStart": 20,
        "skippedUnits": skipped_units,
    }


def build_plot_html(events, child_map, generated_at):
    chart_data = milk_totals_by_day(events, child_map)
    chart_data_json = json_dumps_for_html(chart_data)
    generated = time.strftime("%Y-%m-%d %H:%M", time.localtime(generated_at / 1000))
    default_night_start = int(chart_data.get("defaultNightStart", 20))
    night_start_options = []
    for hour in range(24):
        selected = " selected" if hour == default_night_start else ""
        night_start_options.append(f"<option value=\"{hour}\"{selected}>{hour:02d}:00</option>")
    night_start_options_html = "\n        ".join(night_start_options)
    css = (GLOBAL_CSS + """
    body {
      display: flex;
      justify-content: center;
      align-items: flex-start;
      padding: 16px;
    }
    .container {
      width: min(98vw, 1600px);
      border: 1px solid #2a2a2a;
      border-radius: 10px;
      background: #111;
      padding: clamp(10px, 1.4vw, 20px);
    }
    h1 {
      margin: 0 0 8px 0;
      font-family: var(--font-display);
      font-weight: 400;
      font-size: clamp(20px, 2.2vw, 38px);
    }
    .subtitle {
      margin: 0;
      color: #bdbdbd;
      font-size: clamp(13px, 1vw + 8px, 18px);
    }
    .actions {
      margin: 14px 0;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }
    .btn {
      appearance: none;
      border: 1px solid #2a2a2a;
      background: #1a1a1a;
      color: #f2f2f2;
      padding: 8px 12px;
      font-family: var(--font-body);
      border-radius: 6px;
      cursor: pointer;
      text-decoration: none;
      font-size: clamp(12px, 1vw + 6px, 16px);
    }
    .btn:hover { background: #222; }
    .mode-select {
      border: 1px solid #2a2a2a;
      background: #1a1a1a;
      color: #f2f2f2;
      padding: 8px 12px;
      font-family: var(--font-body);
      border-radius: 6px;
      font-size: clamp(12px, 1vw + 6px, 16px);
    }
    .mode-select:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    .toggle-label {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: #f2f2f2;
      font-size: clamp(12px, 1vw + 6px, 16px);
      cursor: pointer;
      padding: 2px 0;
    }
    .toggle-label input {
      accent-color: #1e88e5;
    }
    .smoothing {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: min(420px, 100%);
    }
    .smooth-slider {
      width: min(240px, 45vw);
      accent-color: #1e88e5;
    }
    .smooth-slider:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    .timeline-legend {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
      margin: 0 0 12px 0;
      color: #d2d2d2;
      font-size: clamp(12px, 1vw + 6px, 16px);
    }
    .timeline-legend[hidden] {
      display: none;
    }
    .timeline-legend-item {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      border: 1px solid #2a2a2a;
      background: #171717;
      color: #d2d2d2;
      border-radius: 6px;
      padding: 4px 7px;
      white-space: nowrap;
      cursor: pointer;
      font: inherit;
    }
    .timeline-legend-item:hover {
      border-color: #4a4a4a;
      background: #202020;
    }
    .timeline-legend-item.is-hidden {
      opacity: 0.45;
      text-decoration: line-through;
    }
    .timeline-legend-emoji {
      position: relative;
      display: inline-grid;
      place-items: center;
      min-width: 1.2em;
      font-size: 1.1em;
      line-height: 1;
    }
    .timeline-legend-emoji-overlay {
      position: absolute;
      right: -0.35em;
      top: -0.35em;
      font-size: 0.72em;
      line-height: 1;
    }
    .chart-wrap {
      position: relative;
      min-height: 58vh;
      height: min(70vh, 760px);
    }
    .chart-wrap canvas {
      width: 100% !important;
      height: 100% !important;
    }
    .meta {
      color: #9e9e9e;
      margin-top: 10px;
      font-size: clamp(12px, 1vw + 6px, 16px);
    }
    @keyframes stale-age-pulse {
      0%, 100% { color: #ffd08a; text-shadow: none; transform: rotate(0deg); }
      50% { color: #ff9f1c; text-shadow: 0 0 12px rgba(255, 159, 28, 0.45); transform: rotate(-1.5deg); }
    }
    .meta-age {
      color: #ffd08a;
      display: inline-block;
      font-size: 1.35em;
      font-weight: 700;
      animation: stale-age-pulse 1.8s ease-in-out infinite;
    }
    .warn {
      color: #ffb74d;
      margin-top: 8px;
      font-size: clamp(12px, 1vw + 6px, 16px);
    }
    """).strip()
    script = f"""
    const payload = {chart_data_json};
    const labels = payload.labels || [];
    const series = payload.series || [];
    const allBabiesGap = payload.allBabiesGap || null;
    const timeline = payload.timeline || {{}};
    const timelineDays = timeline.days || [];
    const timelineChildren = timeline.children || [];
    const timelineTypes = timeline.types || [];
    const timelineNotes = timeline.notes || [];
    const timelinePoints = timeline.points || [];
    let activeLabels = labels;
    const defaultNightStart = Number(payload.defaultNightStart ?? 20);
    const hiddenSeriesKeys = new Set();
    const shownSeriesKeys = new Set();
    const hiddenTimelineTypeKeys = new Set();
    let timelineLayoutCache = null;
    const timelineIconHalfWidthCache = new Map();

    function hourLabel(hour) {{
      return `${{String(hour).padStart(2, "0")}}:00`;
    }}

    function splitWindowText(nightStartHour) {{
      const endHour = (nightStartHour + 12) % 24;
      return `${{hourLabel(nightStartHour)}} to ${{hourLabel(endHour)}}`;
    }}

    function localDayKey(dateValue) {{
      const year = dateValue.getFullYear();
      const month = String(dateValue.getMonth() + 1).padStart(2, "0");
      const day = String(dateValue.getDate()).padStart(2, "0");
      return `${{year}}-${{month}}-${{day}}`;
    }}

    function parseDayKey(dayKey) {{
      if (!dayKey) {{
        return null;
      }}
      const parts = String(dayKey).split("-");
      if (parts.length !== 3) {{
        return null;
      }}
      const year = Number(parts[0]);
      const month = Number(parts[1]);
      const day = Number(parts[2]);
      if ([year, month, day].some((value) => Number.isNaN(value))) {{
        return null;
      }}
      return new Date(year, month - 1, day);
    }}

    function isWeeklyBoundary(index) {{
      const dayDate = parseDayKey(activeLabels[index]);
      return Boolean(dayDate) && dayDate.getDay() === 0;
    }}

    function visibleXRange(scale) {{
      if (!scale) {{
        return {{ min: 0, max: activeLabels.length - 1 }};
      }}
      return {{
        min: Math.max(0, Math.ceil(scale.min ?? 0)),
        max: Math.min(activeLabels.length - 1, Math.floor(scale.max ?? activeLabels.length - 1)),
      }};
    }}

    function tickDataIndex(value, fallbackIndex) {{
      const numericValue = Number(value);
      if (Number.isFinite(numericValue)) {{
        return Math.round(numericValue);
      }}
      return fallbackIndex;
    }}

    function isWeeklyTickValue(value, fallbackIndex) {{
      return isWeeklyBoundary(tickDataIndex(value, fallbackIndex));
    }}

    function weeklyLabelStep(scale) {{
      const visibleRange = visibleXRange(scale);
      let weeklyCount = 0;
      for (let idx = visibleRange.min; idx <= visibleRange.max; idx += 1) {{
        if (isWeeklyBoundary(idx)) {{
          weeklyCount += 1;
        }}
      }}
      const targetLabelCount = Math.max(2, Math.floor(((scale && scale.width) || 0) / 90));
      return Math.max(1, Math.ceil(weeklyCount / targetLabelCount));
    }}

    function visibleDayLabelStep(scale) {{
      const visibleRange = visibleXRange(scale);
      const visibleDayCount = Math.max(1, visibleRange.max - visibleRange.min + 1);
      const targetLabelCount = Math.max(2, Math.floor(((scale && scale.width) || 0) / 90));
      return Math.max(1, Math.ceil(visibleDayCount / targetLabelCount));
    }}

    function weeklyLabelForIndex(index, scale) {{
      if (!isWeeklyBoundary(index)) {{
        return "";
      }}
      const visibleRange = visibleXRange(scale);
      if (index < visibleRange.min || index > visibleRange.max) {{
        return "";
      }}
      const step = weeklyLabelStep(scale);
      let weeklyIndex = 0;
      for (let idx = visibleRange.min; idx < index; idx += 1) {{
        if (isWeeklyBoundary(idx)) {{
          weeklyIndex += 1;
        }}
      }}
      return weeklyIndex % step === 0 ? activeLabels[index] : "";
    }}

    function axisLabelForTickValue(value, fallbackIndex, scale) {{
      const dataIndex = tickDataIndex(value, fallbackIndex);
      const visibleRange = visibleXRange(scale);
      if (dataIndex < visibleRange.min || dataIndex > visibleRange.max) {{
        return "";
      }}
      const visibleDayCount = Math.max(1, visibleRange.max - visibleRange.min + 1);
      if (visibleDayCount <= 28) {{
        const step = visibleDayLabelStep(scale);
        return (dataIndex - visibleRange.min) % step === 0 ? activeLabels[dataIndex] : "";
      }}
      return weeklyLabelForIndex(dataIndex, scale);
    }}

    const todayLabel = localDayKey(new Date());
    const todayIndex = labels.indexOf(todayLabel);

    function hasAnyValue(values) {{
      return Array.isArray(values) && values.some((value) => value != null);
    }}

    function isMilkMode(plotMode) {{
      return plotMode === "milk-daily" || plotMode === "milk-cumulative" || plotMode === "milk-average-feed" || plotMode === "milk-max-feed";
    }}

    function isPercentMode(plotMode) {{
      return plotMode === "milk-breast-percent";
    }}

    function isDiaperMode(plotMode) {{
      return plotMode === "diaper-daily";
    }}

    function isCumulativeMode(plotMode) {{
      return plotMode === "milk-cumulative";
    }}

    function isTimelineMode(plotMode) {{
      return plotMode === "timeline";
    }}

    function isSmoothable(plotMode) {{
      return plotMode !== "milk-cumulative" && !isTimelineMode(plotMode);
    }}

    function diaperMetricLabel(diaperMetric) {{
      if (diaperMetric === "dirty") {{
        return "dirty diapers";
      }}
      if (diaperMetric === "wet") {{
        return "wet diapers";
      }}
      if (diaperMetric === "dry") {{
        return "dry diapers";
      }}
      return "all diapers";
    }}

    function plotModeLabel(plotMode, diaperMetric, milkMetric) {{
      if (plotMode === "milk-cumulative") {{
        return "cumulative milk";
      }}
      if (plotMode === "milk-average-feed") {{
        return "avg milk per feed";
      }}
      if (plotMode === "milk-max-feed") {{
        return "max milk per feed";
      }}
      if (plotMode === "milk-breast-percent") {{
        return "breast milk share";
      }}
      if (plotMode === "diaper-daily") {{
        return diaperMetricLabel(diaperMetric);
      }}
      if (plotMode === "gap-max") {{
        return "max gap";
      }}
      if (plotMode === "gap-avg") {{
        return "avg gap";
      }}
      return `${{milkMetricLabel(milkMetric)}} per day`;
    }}

    function plotUnit(plotMode) {{
      if (isMilkMode(plotMode)) {{
        return "mL";
      }}
      if (isPercentMode(plotMode)) {{
        return "%";
      }}
      if (isDiaperMode(plotMode)) {{
        return "changes";
      }}
      return "h";
    }}

    function plotValueDecimals(plotMode, smoothWindow) {{
      if (isMilkMode(plotMode)) {{
        return 1;
      }}
      if (isPercentMode(plotMode)) {{
        return 1;
      }}
      if (isDiaperMode(plotMode)) {{
        return smoothWindow > 1 ? 1 : 0;
      }}
      return 2;
    }}

    function diaperSeriesField(diaperMetric) {{
      if (diaperMetric === "all") {{
        return "diaperAll";
      }}
      if (diaperMetric === "dirty") {{
        return "diaperDirty";
      }}
      if (diaperMetric === "wet") {{
        return "diaperWet";
      }}
      if (diaperMetric === "dry") {{
        return "diaperDry";
      }}
      return "diaperAll";
    }}

    function diaperMetaField(diaperMetric) {{
      if (diaperMetric === "all") {{
        return "diaperAllMeta";
      }}
      if (diaperMetric === "dirty") {{
        return "diaperDirtyMeta";
      }}
      if (diaperMetric === "wet") {{
        return "diaperWetMeta";
      }}
      if (diaperMetric === "dry") {{
        return "diaperDryMeta";
      }}
      return "diaperAllMeta";
    }}

    function isMilkDailyMode(plotMode) {{
      return plotMode === "milk-daily";
    }}

    function milkMetricLabel(milkMetric) {{
      if (milkMetric === "breast") {{
        return "breast milk";
      }}
      if (milkMetric === "formula") {{
        return "formula";
      }}
      return "all milk";
    }}

    function milkDailyField(milkMetric) {{
      if (milkMetric === "breast") {{
        return "breastDaily";
      }}
      if (milkMetric === "formula") {{
        return "formulaDaily";
      }}
      return "daily";
    }}

    function isRecordHighlightMode(plotMode) {{
      return (
        plotMode === "milk-daily" ||
        plotMode === "milk-average-feed" ||
        plotMode === "milk-max-feed" ||
        plotMode === "gap-avg" ||
        plotMode === "gap-max"
      );
    }}

    function buildPointStyle(plotMode, values, accentColor, diaperMeta) {{
      const pointRadius = [];
      const pointHoverRadius = [];
      const pointBorderWidth = [];
      const pointBackgroundColor = [];
      const pointBorderColor = [];
      let currentRecord = null;

      for (let idx = 0; idx < values.length; idx += 1) {{
        const value = values[idx];
        const hasValue = value != null && !Number.isNaN(value);
        const meta = Array.isArray(diaperMeta) ? diaperMeta[idx] : null;
        const hasBlowout = Boolean(meta && meta.blowoutCount > 0);
        const isRecord = isRecordHighlightMode(plotMode) && hasValue && (currentRecord == null || value > currentRecord);

        if (isRecord) {{
          currentRecord = value;
        }}

        if (!hasValue) {{
          pointRadius.push(0);
          pointHoverRadius.push(0);
          pointBorderWidth.push(0);
          pointBackgroundColor.push(accentColor);
          pointBorderColor.push(accentColor);
          continue;
        }}

        if (hasBlowout) {{
          pointRadius.push(5);
          pointHoverRadius.push(7);
          pointBorderWidth.push(2);
          pointBackgroundColor.push("#111");
          pointBorderColor.push(accentColor);
          continue;
        }}

        pointRadius.push(isRecord ? 4 : 3);
        pointHoverRadius.push(isRecord ? 6 : 5);
        pointBorderWidth.push(isRecord ? 2 : 0);
        pointBackgroundColor.push(isRecord ? "#111" : accentColor);
        pointBorderColor.push(accentColor);
      }}

      return {{
        pointRadius,
        pointHoverRadius,
        pointBorderWidth,
        pointBackgroundColor,
        pointBorderColor,
      }};
    }}

    function movingAverage(values, windowSize, partialDayIndex) {{
      if (windowSize <= 1) {{
        return values.slice();
      }}
      const radius = Math.floor(windowSize / 2);
      const output = new Array(values.length).fill(null);
      for (let idx = 0; idx < values.length; idx += 1) {{
        if (values[idx] == null) {{
          continue;
        }}
        let sum = 0;
        let count = 0;
        const left = Math.max(0, idx - radius);
        const right = Math.min(values.length - 1, idx + radius);
        for (let cursor = left; cursor <= right; cursor += 1) {{
          if (partialDayIndex >= 0 && cursor === partialDayIndex && idx !== partialDayIndex) {{
            continue;
          }}
          const value = values[cursor];
          if (value == null) {{
            continue;
          }}
          sum += value;
          count += 1;
        }}
        output[idx] = count ? Number((sum / count).toFixed(1)) : null;
      }}
      return output;
    }}

    const diaperPlotModes = ["all", "dirty", "wet", "dry"];

    function emptySeries() {{
      return new Array(labels.length).fill(null);
    }}

    function roundPlotValue(value, decimals) {{
      if (value == null || !Number.isFinite(Number(value))) {{
        return null;
      }}
      const factor = 10 ** decimals;
      const scaled = Number(value) * factor;
      const lower = Math.floor(scaled);
      const fraction = scaled - lower;
      let rounded;
      if (Math.abs(fraction - 0.5) < 1e-9) {{
        rounded = lower % 2 === 0 ? lower : lower + 1;
      }} else {{
        rounded = Math.round(scaled);
      }}
      return Number((rounded / factor).toFixed(decimals));
    }}

    function trimMilkSeries(dailyPoints, cumulativePoints) {{
      let firstNonzero = null;
      let lastNonzero = null;
      dailyPoints.forEach((value, idx) => {{
        if (Number(value) > 0) {{
          if (firstNonzero == null) {{
            firstNonzero = idx;
          }}
          lastNonzero = idx;
        }}
      }});
      if (firstNonzero == null) {{
        return null;
      }}

      const daily = [];
      const cumulative = [];
      dailyPoints.forEach((dailyValue, idx) => {{
        if (idx < firstNonzero || idx > lastNonzero) {{
          daily.push(null);
          cumulative.push(idx === firstNonzero - 1 ? 0 : null);
          return;
        }}
        daily.push(roundPlotValue(dailyValue, 1));
        cumulative.push(roundPlotValue(cumulativePoints[idx], 1));
      }});
      return {{ daily, cumulative }};
    }}

    function trimOptionalSeries(values, decimals) {{
      let firstIdx = null;
      let lastIdx = null;
      values.forEach((value, idx) => {{
        if (value != null) {{
          if (firstIdx == null) {{
            firstIdx = idx;
          }}
          lastIdx = idx;
        }}
      }});
      if (firstIdx == null || lastIdx == null) {{
        return null;
      }}

      return values.map((value, idx) => {{
        if (idx < firstIdx || idx > lastIdx || value == null) {{
          return null;
        }}
        return roundPlotValue(value, decimals);
      }});
    }}

    function trimCountSeries(values, decimals) {{
      let firstNonzero = null;
      let lastNonzero = null;
      values.forEach((value, idx) => {{
        if (Number(value) > 0) {{
          if (firstNonzero == null) {{
            firstNonzero = idx;
          }}
          lastNonzero = idx;
        }}
      }});
      if (firstNonzero == null || lastNonzero == null) {{
        return null;
      }}

      return values.map((value, idx) => {{
        if (idx < firstNonzero || idx > lastNonzero) {{
          return null;
        }}
        return roundPlotValue(value, decimals);
      }});
    }}

    function maskSeriesWithNumeric(values, numericMask) {{
      if (!Array.isArray(values) || !Array.isArray(numericMask)) {{
        return null;
      }}
      return values.map((value, idx) => numericMask[idx] != null ? value : null);
    }}

    function isNightHour(hour, nightStartHour) {{
      const nightEndHour = (nightStartHour + 12) % 24;
      if (nightStartHour < nightEndHour) {{
        return nightStartHour <= hour && hour < nightEndHour;
      }}
      return hour >= nightStartHour || hour < nightEndHour;
    }}

    function periodMatchesHour(hour, nightStartHour, period) {{
      if (!period) {{
        return true;
      }}
      const night = isNightHour(hour, nightStartHour);
      return period === "night" ? night : !night;
    }}

    function emptyDiaperStatsByMode() {{
      const output = {{}};
      diaperPlotModes.forEach((mode) => {{
        output[mode] = {{ count: 0, blowoutCount: 0, blowoutDetails: [] }};
      }});
      return output;
    }}

    function addDiaperStats(target, source) {{
      if (!source) {{
        return;
      }}
      diaperPlotModes.forEach((mode) => {{
        const stat = source[mode];
        if (!stat) {{
          return;
        }}
        const targetStat = target[mode];
        targetStat.count += Number.parseInt(stat.count || 0, 10);
        targetStat.blowoutCount += Number.parseInt(stat.blowoutCount || 0, 10);
        (stat.blowoutDetails || []).forEach((detail) => {{
          if (!targetStat.blowoutDetails.includes(detail)) {{
            targetStat.blowoutDetails.push(detail);
          }}
        }});
      }});
    }}

    function aggregateHourlyDay(hourlyByDay, dayKey, nightStartHour, period) {{
      const hours = hourlyByDay && hourlyByDay[dayKey] ? hourlyByDay[dayKey] : {{}};
      const diaper = emptyDiaperStatsByMode();
      const aggregate = {{
        daily: 0,
        breastDaily: 0,
        formulaDaily: 0,
        feedCount: 0,
        maxMilkPerFeed: null,
        breastMilkPercent: null,
        diaper,
        gapSum: 0,
        gapCount: 0,
        maxGap: null,
        maxGapDisplay: null,
        maxGapPeriod: null,
      }};

      Object.entries(hours).forEach(([hourText, hourPayload]) => {{
        const hour = Number.parseInt(hourText, 10);
        if (Number.isNaN(hour) || !periodMatchesHour(hour, nightStartHour, period)) {{
          return;
        }}

        aggregate.daily += Number(hourPayload.daily || 0);
        aggregate.breastDaily += Number(hourPayload.breastDaily || 0);
        aggregate.formulaDaily += Number(hourPayload.formulaDaily || 0);
        aggregate.feedCount += Number.parseInt(hourPayload.feedCount || 0, 10);

        if (hourPayload.maxMilkPerFeed != null) {{
          const maxFeed = Number(hourPayload.maxMilkPerFeed);
          if (Number.isFinite(maxFeed) && (aggregate.maxMilkPerFeed == null || maxFeed > aggregate.maxMilkPerFeed)) {{
            aggregate.maxMilkPerFeed = maxFeed;
          }}
        }}

        addDiaperStats(diaper, hourPayload.diaper);

        const gapCount = Number.parseInt(hourPayload.gapCount || 0, 10);
        if (gapCount > 0) {{
          aggregate.gapSum += Number(hourPayload.gapSum || 0);
          aggregate.gapCount += gapCount;
          const maxGap = Number(hourPayload.maxGap || 0);
          if (aggregate.maxGap == null || maxGap > aggregate.maxGap) {{
            aggregate.maxGap = maxGap;
            aggregate.maxGapDisplay = hourPayload.maxGapDisplay != null ? Number(hourPayload.maxGapDisplay) : maxGap;
            aggregate.maxGapPeriod = hourPayload.maxGapPeriod || null;
          }}
        }}
      }});

      const mixTotal = aggregate.breastDaily + aggregate.formulaDaily;
      if (mixTotal > 0) {{
        aggregate.breastMilkPercent = aggregate.breastDaily * 100 / mixTotal;
      }}
      return aggregate;
    }}

    function deriveHourlySeries(hourlyByDay, nightStartHour, period) {{
      const dailyPoints = [];
      const breastDailyPoints = [];
      const formulaDailyPoints = [];
      const cumulativePoints = [];
      const avgMilkPerFeedPoints = [];
      const maxMilkPerFeedPoints = [];
      const breastMilkPercentPoints = [];
      const diaperPointsByMode = {{}};
      const diaperMetaByMode = {{}};
      const maxGapPoints = [];
      const maxGapPeriods = [];
      const avgGapPoints = [];
      let runningTotal = 0;

      diaperPlotModes.forEach((mode) => {{
        diaperPointsByMode[mode] = [];
        diaperMetaByMode[mode] = [];
      }});

      labels.forEach((dayKey) => {{
        const aggregate = aggregateHourlyDay(hourlyByDay, dayKey, nightStartHour, period);
        runningTotal += aggregate.daily;
        dailyPoints.push(aggregate.daily);
        breastDailyPoints.push(aggregate.breastDaily);
        formulaDailyPoints.push(aggregate.formulaDaily);
        cumulativePoints.push(runningTotal);
        avgMilkPerFeedPoints.push(aggregate.feedCount > 0 ? aggregate.daily / aggregate.feedCount : null);
        maxMilkPerFeedPoints.push(aggregate.maxMilkPerFeed);
        breastMilkPercentPoints.push(aggregate.breastMilkPercent);

        diaperPlotModes.forEach((mode) => {{
          const stat = aggregate.diaper[mode];
          diaperPointsByMode[mode].push(stat.count);
          diaperMetaByMode[mode].push({{
            count: stat.count,
            blowoutCount: stat.blowoutCount,
            blowoutDetails: stat.blowoutDetails.slice(),
          }});
        }});

        if (aggregate.gapCount > 0) {{
          maxGapPoints.push(aggregate.maxGapDisplay);
          maxGapPeriods.push(aggregate.maxGapPeriod);
          avgGapPoints.push(aggregate.gapSum / aggregate.gapCount);
        }} else {{
          maxGapPoints.push(null);
          maxGapPeriods.push(null);
          avgGapPoints.push(null);
        }}
      }});

      const milkSeries = trimMilkSeries(dailyPoints, cumulativePoints);
      const result = {{
        daily: milkSeries ? milkSeries.daily : emptySeries(),
        cumulative: milkSeries ? milkSeries.cumulative : emptySeries(),
        breastDaily: trimCountSeries(breastDailyPoints, 1) || emptySeries(),
        formulaDaily: trimCountSeries(formulaDailyPoints, 1) || emptySeries(),
        avgMilkPerFeed: trimOptionalSeries(avgMilkPerFeedPoints, 1) || emptySeries(),
        maxMilkPerFeed: trimOptionalSeries(maxMilkPerFeedPoints, 1) || emptySeries(),
        breastMilkPercent: trimOptionalSeries(breastMilkPercentPoints, 1) || emptySeries(),
        maxGap: trimOptionalSeries(maxGapPoints, 2) || emptySeries(),
        avgGap: trimOptionalSeries(avgGapPoints, 2) || emptySeries(),
      }};
      result.maxGapPeriod = maskSeriesWithNumeric(maxGapPeriods, result.maxGap) || emptySeries();

      diaperPlotModes.forEach((mode) => {{
        const field = diaperSeriesField(mode);
        const metaField = diaperMetaField(mode);
        const display = trimCountSeries(diaperPointsByMode[mode], 0) || emptySeries();
        result[field] = display;
        result[metaField] = maskSeriesWithNumeric(diaperMetaByMode[mode], display) || emptySeries();
      }});

      return result;
    }}

    function derivedEntrySplit(entry, nightStartHour, period) {{
      if (!entry.$derivedSplits) {{
        entry.$derivedSplits = {{}};
      }}
      const key = `${{nightStartHour}}:${{period}}`;
      if (!entry.$derivedSplits[key]) {{
        entry.$derivedSplits[key] = deriveHourlySeries(entry.hourly || {{}}, nightStartHour, period);
      }}
      return entry.$derivedSplits[key];
    }}

    function derivedAllBabiesGapSplit(nightStartHour, period) {{
      if (!allBabiesGap) {{
        return null;
      }}
      if (!allBabiesGap.$derivedSplits) {{
        allBabiesGap.$derivedSplits = {{}};
      }}
      const key = `${{nightStartHour}}:${{period}}`;
      if (!allBabiesGap.$derivedSplits[key]) {{
        allBabiesGap.$derivedSplits[key] = deriveHourlySeries(allBabiesGap.hourly || {{}}, nightStartHour, period);
      }}
      return allBabiesGap.$derivedSplits[key];
    }}

    function splitSeriesValues(entry, plotMode, diaperMetric, milkMetric, nightStartHour, period) {{
      const split = derivedEntrySplit(entry, nightStartHour, period);
      if (plotMode === "milk-daily") {{
        const milkField = milkDailyField(milkMetric);
        return split[milkField] || [];
      }}
      if (plotMode === "milk-cumulative") {{
        return split.cumulative || [];
      }}
      if (plotMode === "milk-average-feed") {{
        return split.avgMilkPerFeed || [];
      }}
      if (plotMode === "milk-max-feed") {{
        return split.maxMilkPerFeed || [];
      }}
      if (plotMode === "milk-breast-percent") {{
        return split.breastMilkPercent || [];
      }}
      if (plotMode === "gap-max") {{
        return split.maxGap || [];
      }}
      if (plotMode === "gap-avg") {{
        return split.avgGap || [];
      }}
      if (isDiaperMode(plotMode)) {{
        const diaperField = diaperSeriesField(diaperMetric);
        return split[diaperField] || [];
      }}
      return split.daily || [];
    }}

    function splitSeriesDiaperMeta(entry, diaperMetric, nightStartHour, period) {{
      const split = derivedEntrySplit(entry, nightStartHour, period);
      const metaField = diaperMetaField(diaperMetric);
      return split[metaField] || [];
    }}

    function splitSeriesMaxGapPeriods(entry, nightStartHour, period) {{
      const split = derivedEntrySplit(entry, nightStartHour, period);
      return split.maxGapPeriod || [];
    }}

    function modeSeriesValues(entry, plotMode, diaperMetric, milkMetric) {{
      if (plotMode === "milk-daily") {{
        const milkField = milkDailyField(milkMetric);
        return entry[milkField] || [];
      }}
      if (plotMode === "milk-cumulative") {{
        return entry.cumulative || [];
      }}
      if (plotMode === "milk-average-feed") {{
        return entry.avgMilkPerFeed || [];
      }}
      if (plotMode === "milk-max-feed") {{
        return entry.maxMilkPerFeed || [];
      }}
      if (plotMode === "milk-breast-percent") {{
        return entry.breastMilkPercent || [];
      }}
      if (plotMode === "gap-max") {{
        return entry.maxGap || [];
      }}
      if (plotMode === "gap-avg") {{
        return entry.avgGap || [];
      }}
      if (isDiaperMode(plotMode)) {{
        const diaperField = diaperSeriesField(diaperMetric);
        return entry[diaperField] || [];
      }}
      return entry.daily || [];
    }}

    function modeSeriesDiaperMeta(entry, diaperMetric) {{
      const metaField = diaperMetaField(diaperMetric);
      return entry[metaField] || [];
    }}

    function modeSeriesMaxGapPeriods(entry) {{
      return entry.maxGapPeriod || [];
    }}

    function allBabiesModeGapValues(plotMode) {{
      if (!allBabiesGap) {{
        return [];
      }}
      if (plotMode === "gap-max") {{
        return allBabiesGap.maxGap || [];
      }}
      if (plotMode === "gap-avg") {{
        return allBabiesGap.avgGap || [];
      }}
      return [];
    }}

    function allBabiesModeGapPeriods() {{
      if (!allBabiesGap) {{
        return [];
      }}
      return allBabiesGap.maxGapPeriod || [];
    }}

    function allBabiesSplitGapValues(plotMode, nightStartHour, period) {{
      if (!allBabiesGap) {{
        return [];
      }}
      const split = derivedAllBabiesGapSplit(nightStartHour, period);
      if (plotMode === "gap-max") {{
        return split.maxGap || [];
      }}
      if (plotMode === "gap-avg") {{
        return split.avgGap || [];
      }}
      return [];
    }}

    function allBabiesSplitGapPeriods(nightStartHour, period) {{
      if (!allBabiesGap) {{
        return [];
      }}
      const split = derivedAllBabiesGapSplit(nightStartHour, period);
      return split.maxGapPeriod || [];
    }}

    function allBabiesDailyMilkTotal(plotMode, diaperMetric, milkMetric, splitEnabled, nightStartHour, period, dataIndex) {{
      if (plotMode !== "milk-daily" || dataIndex == null) {{
        return null;
      }}
      let total = 0;
      let count = 0;
      series.forEach((entry) => {{
        const values = splitEnabled
          ? splitSeriesValues(entry, plotMode, diaperMetric, milkMetric, nightStartHour, period || "day")
          : modeSeriesValues(entry, plotMode, diaperMetric, milkMetric);
        const value = values[dataIndex];
        if (typeof value !== "number" || !Number.isFinite(value)) {{
          return;
        }}
        total += value;
        count += 1;
      }});
      if (!count) {{
        return null;
      }}
      return total;
    }}

    function allBabiesDailyMilkSeries(plotMode, diaperMetric, milkMetric, splitEnabled, nightStartHour, period) {{
      if (plotMode !== "milk-daily") {{
        return [];
      }}
      return labels.map((_, dataIndex) => allBabiesDailyMilkTotal(
        plotMode,
        diaperMetric,
        milkMetric,
        splitEnabled,
        nightStartHour,
        period,
        dataIndex
      ));
    }}

    function formatMinuteOfDay(minute) {{
      if (minute == null || !Number.isFinite(Number(minute))) {{
        return "";
      }}
      const normalized = ((Math.round(Number(minute)) % 1440) + 1440) % 1440;
      const hour = Math.floor(normalized / 60);
      const min = normalized % 60;
      return `${{String(hour).padStart(2, "0")}}:${{String(min).padStart(2, "0")}}`;
    }}

    function timelineValueText(point) {{
      if (point.value == null) {{
        return "";
      }}
      const type = timelineTypes[point.typeIndex] || {{}};
      if (type.key === "feed") {{
        return `${{point.value}} mL`;
      }}
      if (type.key === "growth") {{
        return `${{point.value}} lb`;
      }}
      return String(point.value);
    }}

    const timelineTypeOrder = [
      "feed",
      "album",
      "diaper-dry",
      "diaper-wet",
      "diaper-dirty",
      "diaper-blowout",
      "pump",
      "vitamin",
      "medication",
      "bath",
      "routine",
      "sleep",
      "growth",
      "medical",
      "diaper",
    ];
    const timelineTypeOrderIndex = new Map(timelineTypeOrder.map((key, idx) => [key, idx]));

    function timelineTypeSortValue(typeIndex) {{
      const type = timelineTypes[typeIndex] || {{}};
      const order = timelineTypeOrderIndex.get(type.key);
      return order == null ? timelineTypeOrder.length + typeIndex : order;
    }}

    const timelineColumnGroupOrder = [
      "feed",
      "other",
      "diaper",
    ];
    const timelineColumnGroupOrderIndex = new Map(timelineColumnGroupOrder.map((key, idx) => [key, idx]));

    function timelineColumnGroupKey(typeIndex) {{
      const type = timelineTypes[typeIndex] || {{}};
      const key = type.key || "other";
      if (key === "feed" || key === "album") {{
        return "feed";
      }}
      if (key === "diaper" || key.startsWith("diaper-")) {{
        return "diaper";
      }}
      return "other";
    }}

    function timelineColumnGroupSortValue(groupKey) {{
      const order = timelineColumnGroupOrderIndex.get(groupKey);
      return order == null ? timelineColumnGroupOrder.length : order;
    }}

    function timelineOverlayEmoji(typeKey) {{
      return typeKey === "diaper-blowout" ? "💥" : "";
    }}

    function timelineVerticalCollisionMinutes() {{
      const chartArea = chart && chart.chartArea;
      const chartHeight = chartArea ? chartArea.bottom - chartArea.top : 0;
      const canvasHeight = canvas && (canvas.clientHeight || canvas.height);
      const height = chartHeight > 0 ? chartHeight : canvasHeight > 0 ? canvasHeight : 470;
      return (1440 / height) * timelineIconHeightPx() * 1.15;
    }}

    function timelineBandPixelWidth() {{
      const xScale = chart && chart.scales && chart.scales.x;
      const {{ maxBandWidth }} = timelineBandLayout();
      const bandWidth = maxBandWidth || 1;
      const chartArea = chart && chart.chartArea;
      const chartWidth = chartArea ? chartArea.right - chartArea.left : 0;
      const width = chartWidth > 0 ? chartWidth : canvas && canvas.clientWidth ? canvas.clientWidth : 900;
      const scaleMin = xScale && Number.isFinite(Number(xScale.min)) ? Number(xScale.min) : 0;
      const scaleMax = xScale && Number.isFinite(Number(xScale.max)) ? Number(xScale.max) : Math.max(1, timelineDays.length);
      const range = Math.max(1, scaleMax - scaleMin);
      return (width / range) * bandWidth;
    }}

    function timelineEmojiFontSize() {{
      return Math.min(24, Math.max(7, Math.floor(timelineBandPixelWidth() * 0.45)));
    }}

    function timelineEmojiFont(fontSize = timelineEmojiFontSize()) {{
      return `${{fontSize}}px system-ui, Apple Color Emoji, Segoe UI Emoji, Noto Color Emoji, sans-serif`;
    }}

    function timelineIconHeightPx() {{
      return timelineEmojiFontSize() * 1.8;
    }}

    function timelineIconHalfWidthPx(typeIndex = null) {{
      const fontSize = timelineEmojiFontSize();
      const cacheKey = `${{fontSize}}:${{typeIndex == null ? "all" : typeIndex}}`;
      if (timelineIconHalfWidthCache.has(cacheKey)) {{
        return timelineIconHalfWidthCache.get(cacheKey);
      }}
      const ctx = chart && chart.ctx;
      if (!ctx || typeof ctx.measureText !== "function") {{
        const fallbackWidth = fontSize * 0.85;
        timelineIconHalfWidthCache.set(cacheKey, fallbackWidth);
        return fallbackWidth;
      }}
      const previousFont = ctx.font;
      ctx.font = timelineEmojiFont(fontSize);
      let halfWidth = fontSize * 0.55;
      const typesToMeasure = typeIndex == null ? timelineTypes : [timelineTypes[typeIndex] || {{ emoji: "•" }}];
      typesToMeasure.forEach((type) => {{
        const measured = ctx.measureText(type.emoji || "•");
        const width = Number(measured && measured.width) || 0;
        const left = Number(measured && measured.actualBoundingBoxLeft) || 0;
        const right = Number(measured && measured.actualBoundingBoxRight) || 0;
        const measuredHalfWidth = left > 0 || right > 0 ? Math.max(left, right) : width / 2;
        halfWidth = Math.max(halfWidth, measuredHalfWidth);
      }});
      ctx.font = previousFont;
      const iconHalfWidth = Math.min(halfWidth, fontSize * 0.62) + 1;
      timelineIconHalfWidthCache.set(cacheKey, iconHalfWidth);
      return iconHalfWidth;
    }}

    function timelineIconPaddingXUnits(typeIndex = null) {{
      const xScale = chart && chart.scales && chart.scales.x;
      const chartArea = chart && chart.chartArea;
      const chartWidth = chartArea ? chartArea.right - chartArea.left : 0;
      const scaleMin = xScale && Number.isFinite(Number(xScale.min)) ? Number(xScale.min) : 0;
      const scaleMax = xScale && Number.isFinite(Number(xScale.max)) ? Number(xScale.max) : Math.max(1, timelineDays.length);
      const range = Math.max(1, scaleMax - scaleMin);
      const width = chartWidth > 0 ? chartWidth : canvas && canvas.clientWidth ? canvas.clientWidth : 900;
      return (range / width) * timelineIconHalfWidthPx(typeIndex);
    }}

    function timelineChildKey(child) {{
      return `timeline:${{child.label}}`;
    }}

    function timelinePointIntersectsXRange(point, xMin, xMax) {{
      const dayIndex = Number(point[0]);
      return Number.isFinite(dayIndex) && dayIndex < xMax && dayIndex + 1 > xMin;
    }}

    function timelineSortedColumnGroups(groupKeys) {{
      return Array.from(groupKeys)
        .sort((a, b) => timelineColumnGroupSortValue(a) - timelineColumnGroupSortValue(b) || a.localeCompare(b));
    }}

    function timelineRangeForScale() {{
      const xScale = chart && chart.scales && chart.scales.x;
      const xMin = xScale && Number.isFinite(Number(xScale.min)) ? Number(xScale.min) : 0;
      const xMax = xScale && Number.isFinite(Number(xScale.max)) ? Number(xScale.max) : Math.max(1, timelineDays.length);
      return {{ xMin, xMax }};
    }}

    function timelineLayoutCacheKey(xMin, xMax) {{
      return [
        xMin,
        xMax,
        Array.from(hiddenSeriesKeys).sort().join(","),
        Array.from(shownSeriesKeys).sort().join(","),
        Array.from(hiddenTimelineTypeKeys).sort().join(","),
      ].join("|");
    }}

    function timelineVisibleSummary() {{
      const {{ xMin, xMax }} = timelineRangeForScale();
      const cacheKey = timelineLayoutCacheKey(xMin, xMax);
      if (timelineLayoutCache && timelineLayoutCache.key === cacheKey) {{
        return timelineLayoutCache.value;
      }}
      const groupKeys = new Set();
      const presentTypeIndexes = new Set();
      const presentChildIndexes = new Set();
      const presentChildGroupSets = new Map();
      const childGroupSets = new Map();
      const visibleTypeIndexes = new Set();
      timelinePoints.forEach((point) => {{
        if (!timelinePointIntersectsXRange(point, xMin, xMax)) {{
          return;
        }}
        const childIndex = Number(point[2]);
        const child = timelineChildren[childIndex];
        if (!child) {{
          return;
        }}
        const typeIndex = Number(point[3]);
        const type = timelineTypes[typeIndex] || {{ key: "event" }};
        const groupKey = timelineColumnGroupKey(typeIndex);
        presentChildIndexes.add(childIndex);
        presentTypeIndexes.add(typeIndex);
        if (!presentChildGroupSets.has(childIndex)) {{
          presentChildGroupSets.set(childIndex, new Set());
        }}
        presentChildGroupSets.get(childIndex).add(groupKey);
        if (isDatasetHidden(timelineChildKey(child), false)) {{
          return;
        }}
        if (hiddenTimelineTypeKeys.has(type.key || "event")) {{
          return;
        }}
        groupKeys.add(groupKey);
        visibleTypeIndexes.add(typeIndex);
        if (!childGroupSets.has(childIndex)) {{
          childGroupSets.set(childIndex, new Set());
        }}
        childGroupSets.get(childIndex).add(groupKey);
      }});
      const presentChildGroups = new Map();
      presentChildGroupSets.forEach((groups, childIndex) => {{
        presentChildGroups.set(childIndex, timelineSortedColumnGroups(groups));
      }});
      const childGroups = new Map();
      childGroupSets.forEach((groups, childIndex) => {{
        childGroups.set(childIndex, timelineSortedColumnGroups(groups));
      }});
      const summary = {{
        xMin,
        xMax,
        presentTypeIndexes: Array.from(presentTypeIndexes).sort((a, b) => timelineTypeSortValue(a) - timelineTypeSortValue(b) || a - b),
        presentChildIndexes: timelineChildren
          .map((child, idx) => idx)
          .filter((idx) => presentChildIndexes.has(idx)),
        presentChildGroups,
        visibleGroupKeys: timelineSortedColumnGroups(groupKeys),
        visibleTypeIndexes: Array.from(visibleTypeIndexes).sort((a, b) => timelineTypeSortValue(a) - timelineTypeSortValue(b) || a - b),
        visibleChildIndexes: timelineChildren
          .map((child, idx) => idx)
          .filter((idx) => childGroups.has(idx)),
        childGroups,
      }};
      timelineLayoutCache = {{ key: cacheKey, value: summary }};
      return summary;
    }}

    function timelineVisibleColumnGroups() {{
      return timelineVisibleSummary().visibleGroupKeys;
    }}

    function timelineVisibleIconPaddingXUnits() {{
      let padding = 0;
      timelineVisibleSummary().visibleTypeIndexes.forEach((typeIndex) => {{
        padding = Math.max(padding, timelineIconPaddingXUnits(typeIndex));
      }});
      return padding || timelineIconPaddingXUnits();
    }}

    function timelineBandLayout() {{
      const summary = timelineVisibleSummary();
      const visibleChildIndexes = summary.visibleChildIndexes;
      const separatorWeight = visibleChildIndexes.length ? 1 : 0;
      const totalWeight = visibleChildIndexes.reduce((total, childIndex) => total + Math.max(1, (summary.childGroups.get(childIndex) || []).length), separatorWeight) || 1;
      const bandByChild = new Map();
      let bandStart = 0;
      let maxBandWidth = 0;
      visibleChildIndexes.forEach((childIndex, bandIndex) => {{
        const bandWidth = Math.max(1, (summary.childGroups.get(childIndex) || []).length) / totalWeight;
        maxBandWidth = Math.max(maxBandWidth, bandWidth);
        bandByChild.set(childIndex, {{
          bandIndex,
          bandWidth,
          startOffset: bandStart,
          centerOffset: bandStart + bandWidth / 2,
        }});
        bandStart += bandWidth;
      }});
      return {{ visibleChildIndexes, bandByChild, separatorStart: bandStart, separatorWidth: separatorWeight / totalWeight, maxBandWidth }};
    }}

    function timelineClusterOffset(order, count, bandWidth, centerLimit) {{
      if (count <= 1) {{
        return 0;
      }}
      if (centerLimit <= 0) {{
        return 0;
      }}
      return -centerLimit + (2 * centerLimit * order) / (count - 1);
    }}

    function timelineSlotOffset(slotIndex, slotCount, leftLimit, rightLimit) {{
      if (slotCount <= 1) {{
        return (leftLimit + rightLimit) / 2;
      }}
      return leftLimit + ((rightLimit - leftLimit) * slotIndex) / (slotCount - 1);
    }}

    function timelineLaneOffset(order, count, groupIndex, groupCount, slotSpacing) {{
      if (count <= 1 || slotSpacing <= 0) {{
        return 0;
      }}
      const spread = slotSpacing * 0.42;
      if (groupCount <= 1) {{
        return -spread / 2 + (spread * order) / (count - 1);
      }}
      if (groupIndex === 0) {{
        return (spread * order) / (count - 1);
      }}
      if (groupIndex === groupCount - 1) {{
        return -spread + (spread * order) / (count - 1);
      }}
      return -spread / 2 + (spread * order) / (count - 1);
    }}

    function assignTimelineXOffsets(validPoints, bandLayout) {{
      const byDayChild = new Map();
      const visibleGroupKeys = timelineVisibleColumnGroups();
      const visiblePadding = timelineVisibleIconPaddingXUnits();
      validPoints.forEach((entry, index) => {{
        entry.inputIndex = index;
        const key = `${{entry.dayIndex}}:${{entry.childIndex}}`;
        if (!byDayChild.has(key)) {{
          byDayChild.set(key, []);
        }}
        byDayChild.get(key).push(entry);
      }});

      const offsets = new Map();
      byDayChild.forEach((entries) => {{
        const childIndex = entries[0] && entries[0].childIndex;
        const band = bandLayout.bandByChild.get(childIndex);
        const bandWidth = band ? band.bandWidth : 1;
        const yScale = chart && chart.scales && chart.scales.y;
        const minPixelGap = timelineIconHeightPx() * 0.95;
        const byGroup = new Map();
        entries.forEach((entry) => {{
          const groupKey = timelineColumnGroupKey(entry.typeIndex);
          if (!byGroup.has(groupKey)) {{
            byGroup.set(groupKey, []);
          }}
          byGroup.get(groupKey).push(entry);
        }});

        const groupKeys = visibleGroupKeys.length
          ? visibleGroupKeys
          : Array.from(byGroup.keys()).sort((a, b) => timelineColumnGroupSortValue(a) - timelineColumnGroupSortValue(b) || a.localeCompare(b));
        const groupCount = Math.max(1, groupKeys.length);
        const bandPadding = Math.min(visiblePadding, bandWidth / 2);
        const leftLimit = -bandWidth / 2 + bandPadding;
        const rightLimit = bandWidth / 2 - bandPadding;
        const slotSpacing = groupCount > 1 ? (rightLimit - leftLimit) / (groupCount - 1) : rightLimit - leftLimit;
        const groupCenterByKey = new Map();
        const groupIndexByKey = new Map();
        groupKeys.forEach((groupKey, groupIndex) => {{
          groupCenterByKey.set(groupKey, timelineSlotOffset(groupIndex, groupCount, leftLimit, rightLimit));
          groupIndexByKey.set(groupKey, groupIndex);
        }});

        byGroup.forEach((groupEntries, groupKey) => {{
          const lanes = [];
          const sortedEntries = groupEntries
            .slice()
            .sort((a, b) =>
              (yScale && typeof yScale.getPixelForValue === "function"
                ? yScale.getPixelForValue(a.minute) - yScale.getPixelForValue(b.minute)
                : a.minute - b.minute) ||
              a.minute - b.minute ||
              timelineTypeSortValue(a.typeIndex) - timelineTypeSortValue(b.typeIndex) ||
              a.inputIndex - b.inputIndex
            );
          sortedEntries.forEach((entry) => {{
            const y = yScale && typeof yScale.getPixelForValue === "function"
              ? yScale.getPixelForValue(entry.minute)
              : entry.minute;
            let laneIndex = lanes.findIndex((lane) => Math.abs(y - lane.lastY) >= minPixelGap);
            if (laneIndex < 0) {{
              laneIndex = lanes.length;
              lanes.push({{ lastY: y }});
            }} else {{
              lanes[laneIndex].lastY = y;
            }}
            entry.timelineLane = laneIndex;
          }});
          const laneCount = Math.max(1, lanes.length);
          const groupCenter = groupCenterByKey.get(groupKey) || 0;
          const groupIndex = groupIndexByKey.get(groupKey) || 0;
          sortedEntries.forEach((entry) => {{
            const rawOffset = groupCenter + (
              groupKey === "other"
                ? timelineLaneOffset(entry.timelineLane || 0, laneCount, groupIndex, groupCount, slotSpacing)
                : 0
            );
            const entryPadding = Math.min(timelineIconPaddingXUnits(entry.typeIndex), bandWidth / 2);
            const leftLimit = -bandWidth / 2 + entryPadding;
            const rightLimit = bandWidth / 2 - entryPadding;
            offsets.set(
              entry.point,
              Math.max(leftLimit, Math.min(rightLimit, rawOffset))
            );
          }});
        }});
      }});
      return offsets;
    }}

    function buildTimelineDatasets() {{
      const byChild = new Map();
      timelineChildren.forEach((child, idx) => {{
        byChild.set(idx, {{
          child,
          data: [],
        }});
      }});

      const validPoints = [];
      timelinePoints.forEach((point) => {{
        const dayIndex = Number(point[0]);
        const minute = Number(point[1]);
        const childIndex = Number(point[2]);
        const typeIndex = Number(point[3]);
        if (
          !Number.isFinite(dayIndex) ||
          !Number.isFinite(minute) ||
          !Number.isFinite(typeIndex) ||
          !timelineDays[dayIndex] ||
          !byChild.has(childIndex)
        ) {{
          return;
        }}
        const type = timelineTypes[typeIndex] || {{ key: "event" }};
        if (hiddenTimelineTypeKeys.has(type.key || "event")) {{
          return;
        }}
        const entry = {{ point, dayIndex, minute, childIndex, typeIndex }};
        validPoints.push(entry);
      }});

      const bandLayout = timelineBandLayout();
      const xOffsets = assignTimelineXOffsets(validPoints, bandLayout);

      validPoints.forEach((entry) => {{
        const {{ point, dayIndex, minute, childIndex, typeIndex }} = entry;
        const type = timelineTypes[typeIndex] || {{ emoji: "•", label: "Event", key: "event" }};
        const noteIndex = point[6];
        const band = bandLayout.bandByChild.get(childIndex);
        if (!band) {{
          return;
        }}
        const centerOffset = band ? band.centerOffset : 0.5;
        byChild.get(childIndex).data.push({{
          x: dayIndex + centerOffset + (xOffsets.get(point) || 0),
          y: minute,
          dayLabel: timelineDays[dayIndex],
          childIndex,
          typeIndex,
          typeKey: type.key || "event",
          emoji: type.emoji || "•",
          overlayEmoji: timelineOverlayEmoji(type.key || "event"),
          typeLabel: type.label || "Event",
          endMinute: point[4],
          value: point[5],
          note: noteIndex == null ? null : timelineNotes[noteIndex] || null,
        }});
      }});

      const datasets = [];
      const presentChildIndexes = new Set(timelineVisibleSummary().presentChildIndexes);
      byChild.forEach((entry, childIndex) => {{
        if (!entry.data.length && !presentChildIndexes.has(childIndex)) {{
          return;
        }}
        const customKey = timelineChildKey(entry.child);
        const defaultHidden = false;
        datasets.push({{
          type: "scatter",
          label: entry.child.label,
          customKey,
          data: entry.data,
          borderColor: entry.child.borderColor,
          backgroundColor: entry.child.backgroundColor,
          showLine: false,
          pointRadius: 7,
          pointHoverRadius: 10,
          pointHitRadius: 12,
          pointBorderWidth: 2,
          pointBackgroundColor: "rgba(0,0,0,0)",
          pointBorderColor: "rgba(0,0,0,0)",
          hidden: isDatasetHidden(customKey, defaultHidden),
          defaultHidden,
        }});
      }});
      return datasets;
    }}

    function isDatasetHidden(customKey, defaultHidden) {{
      if (hiddenSeriesKeys.has(customKey)) {{
        return true;
      }}
      if (defaultHidden && !shownSeriesKeys.has(customKey)) {{
        return true;
      }}
      return false;
    }}

    function buildDatasets(plotMode, diaperMetric, milkMetric, smoothWindow, splitEnabled, nightStartHour) {{
      if (isTimelineMode(plotMode)) {{
        return buildTimelineDatasets();
      }}
      const datasets = [];
      series.forEach((entry) => {{
        if (!splitEnabled) {{
          const raw = modeSeriesValues(entry, plotMode, diaperMetric, milkMetric);
          const diaperMeta = isDiaperMode(plotMode) ? modeSeriesDiaperMeta(entry, diaperMetric) : null;
          const maxGapPeriods = plotMode === "gap-max" ? modeSeriesMaxGapPeriods(entry) : null;
          const data = isSmoothable(plotMode) ? movingAverage(raw, smoothWindow, todayIndex) : raw;
          if (!hasAnyValue(data)) {{
            return;
          }}
          const customKey = `single:${{plotMode}}:${{diaperMetric}}:${{milkMetric}}:${{entry.label}}`;
          const defaultHidden = false;
          const pointStyle = buildPointStyle(plotMode, data, entry.borderColor, diaperMeta);
          datasets.push({{
            label: entry.label,
            customKey,
            data,
            borderColor: entry.borderColor,
            backgroundColor: entry.backgroundColor,
            pointRadius: pointStyle.pointRadius,
            pointHoverRadius: pointStyle.pointHoverRadius,
            pointBorderWidth: pointStyle.pointBorderWidth,
            pointBackgroundColor: pointStyle.pointBackgroundColor,
            pointBorderColor: pointStyle.pointBorderColor,
            borderWidth: 2,
            tension: 0.2,
            spanGaps: false,
            hidden: isDatasetHidden(customKey, defaultHidden),
            defaultHidden,
            dailyPeriod: null,
            maxGapPeriods,
            diaperMeta,
          }});
          return;
        }}

        const periodSpecs = [
          {{ period: "day", label: "Day", dash: [] }},
          {{ period: "night", label: "Night", dash: [8, 5] }},
        ];
        periodSpecs.forEach((spec) => {{
          const raw = splitSeriesValues(entry, plotMode, diaperMetric, milkMetric, nightStartHour, spec.period);
          const diaperMeta = isDiaperMode(plotMode)
            ? splitSeriesDiaperMeta(entry, diaperMetric, nightStartHour, spec.period)
            : null;
          const maxGapPeriods =
            plotMode === "gap-max" ? splitSeriesMaxGapPeriods(entry, nightStartHour, spec.period) : null;
          const data = isSmoothable(plotMode) ? movingAverage(raw, smoothWindow, todayIndex) : raw;
          if (!hasAnyValue(data)) {{
            return;
          }}
          const customKey = `split:${{plotMode}}:${{diaperMetric}}:${{milkMetric}}:${{entry.label}}:${{spec.period}}`;
          const defaultHidden = false;
          const pointStyle = buildPointStyle(plotMode, data, entry.borderColor, diaperMeta);
          datasets.push({{
            label: `${{entry.label}} (${{spec.label}})`,
            customKey,
            data,
            borderColor: entry.borderColor,
            backgroundColor: entry.backgroundColor,
            borderDash: spec.dash,
            pointRadius: pointStyle.pointRadius,
            pointHoverRadius: pointStyle.pointHoverRadius,
            pointBorderWidth: pointStyle.pointBorderWidth,
            pointBackgroundColor: pointStyle.pointBackgroundColor,
            pointBorderColor: pointStyle.pointBorderColor,
            borderWidth: 2,
            tension: 0.2,
            spanGaps: false,
            hidden: isDatasetHidden(customKey, defaultHidden),
            defaultHidden,
            dailyPeriod: spec.period,
            maxGapPeriods,
            diaperMeta,
          }});
        }});
      }});

      if (plotMode === "milk-daily") {{
        const combinedPeriodSpecs = splitEnabled
          ? [
              {{ period: "day", label: "Day", dash: [] }},
              {{ period: "night", label: "Night", dash: [8, 5] }},
            ]
          : [{{ period: null, label: "", dash: [] }}];
        combinedPeriodSpecs.forEach((spec) => {{
          const raw = allBabiesDailyMilkSeries(
            plotMode,
            diaperMetric,
            milkMetric,
            splitEnabled,
            nightStartHour,
            spec.period
          );
          const data = isSmoothable(plotMode) ? movingAverage(raw, smoothWindow, todayIndex) : raw;
          if (!hasAnyValue(data)) {{
            return;
          }}
          const customKey = splitEnabled
            ? `combined:${{plotMode}}:${{milkMetric}}:all-babies:${{spec.period}}`
            : `combined:${{plotMode}}:${{milkMetric}}:all-babies`;
          const defaultHidden = true;
          const pointStyle = buildPointStyle(plotMode, data, "#ffffff", null);
          datasets.push({{
            label: spec.period ? `All Babies (${{spec.label}})` : "All Babies",
            customKey,
            data,
            borderColor: "#ffffff",
            backgroundColor: "#ffffff",
            borderDash: spec.dash,
            pointRadius: pointStyle.pointRadius,
            pointHoverRadius: pointStyle.pointHoverRadius,
            pointBorderWidth: pointStyle.pointBorderWidth,
            pointBackgroundColor: pointStyle.pointBackgroundColor,
            pointBorderColor: pointStyle.pointBorderColor,
            borderWidth: 3,
            tension: 0.2,
            spanGaps: false,
            hidden: isDatasetHidden(customKey, defaultHidden),
            defaultHidden,
            dailyPeriod: spec.period,
          }});
        }});
      }}

      const isGapMode = plotMode === "gap-max" || plotMode === "gap-avg";
      if (!isGapMode || !allBabiesGap) {{
        return datasets;
      }}

      if (!splitEnabled) {{
        const raw = allBabiesModeGapValues(plotMode);
        const maxGapPeriods = plotMode === "gap-max" ? allBabiesModeGapPeriods() : null;
        const data = isSmoothable(plotMode) ? movingAverage(raw, smoothWindow, todayIndex) : raw;
        if (hasAnyValue(data)) {{
          const customKey = `combined:${{plotMode}}:all-babies`;
          const defaultHidden = false;
          const pointStyle = buildPointStyle(plotMode, data, "#ffffff", null);
          datasets.push({{
            label: allBabiesGap.label || "All Babies",
            customKey,
            data,
            borderColor: "#ffffff",
            backgroundColor: "#ffffff",
            pointRadius: pointStyle.pointRadius,
            pointHoverRadius: pointStyle.pointHoverRadius,
            pointBorderWidth: pointStyle.pointBorderWidth,
            pointBackgroundColor: pointStyle.pointBackgroundColor,
            pointBorderColor: pointStyle.pointBorderColor,
            borderWidth: 3,
            tension: 0.2,
            spanGaps: false,
            hidden: isDatasetHidden(customKey, defaultHidden),
            defaultHidden,
            maxGapPeriods,
          }});
        }}
        return datasets;
      }}

      const combinedPeriodSpecs = [
        {{ period: "day", label: "Day", dash: [] }},
        {{ period: "night", label: "Night", dash: [8, 5] }},
      ];
      combinedPeriodSpecs.forEach((spec) => {{
        const raw = allBabiesSplitGapValues(plotMode, nightStartHour, spec.period);
        const maxGapPeriods =
          plotMode === "gap-max" ? allBabiesSplitGapPeriods(nightStartHour, spec.period) : null;
        const data = isSmoothable(plotMode) ? movingAverage(raw, smoothWindow, todayIndex) : raw;
        if (!hasAnyValue(data)) {{
          return;
        }}
        const customKey = `combined:${{plotMode}}:all-babies:${{spec.period}}`;
        const defaultHidden = false;
        const labelBase = allBabiesGap.label || "All Babies";
        const pointStyle = buildPointStyle(plotMode, data, "#ffffff", null);
        datasets.push({{
          label: `${{labelBase}} (${{spec.label}})`,
          customKey,
          data,
          borderColor: "#ffffff",
          backgroundColor: "#ffffff",
          borderDash: spec.dash,
          pointRadius: pointStyle.pointRadius,
          pointHoverRadius: pointStyle.pointHoverRadius,
          pointBorderWidth: pointStyle.pointBorderWidth,
          pointBackgroundColor: pointStyle.pointBackgroundColor,
          pointBorderColor: pointStyle.pointBorderColor,
          borderWidth: 3,
          tension: 0.2,
          spanGaps: false,
          hidden: isDatasetHidden(customKey, defaultHidden),
          defaultHidden,
          maxGapPeriods,
        }});
      }});
      return datasets;
    }}

    function yAxisTitle(plotMode, diaperMetric, milkMetric, smoothWindow, splitEnabled, nightStartHour) {{
      let baseTitle = "";
      if (isTimelineMode(plotMode)) {{
        baseTitle = "Time of day";
      }} else if (plotMode === "milk-cumulative") {{
        baseTitle = "Cumulative milk eaten (mL)";
      }} else if (plotMode === "milk-average-feed") {{
        baseTitle = smoothWindow <= 1
          ? "Average milk per feed (mL)"
          : `Average milk per feed (mL, ${{smoothWindow}}-day moving avg)`;
      }} else if (plotMode === "milk-max-feed") {{
        baseTitle = smoothWindow <= 1
          ? "Max milk per feed (mL)"
          : `Max milk per feed (mL, ${{smoothWindow}}-day moving avg)`;
      }} else if (plotMode === "milk-breast-percent") {{
        baseTitle = smoothWindow <= 1
          ? "Breast milk share of bottle intake (%)"
          : `Breast milk share of bottle intake (%, ${{smoothWindow}}-day moving avg)`;
      }} else if (plotMode === "diaper-daily") {{
        baseTitle = `${{diaperMetricLabel(diaperMetric).replace(/^./, (c) => c.toUpperCase())}} per day`;
      }} else if (plotMode === "gap-max") {{
        baseTitle = "Max feeding gap per day (hours)";
      }} else if (plotMode === "gap-avg") {{
        baseTitle = "Average feeding gap per day (hours)";
      }} else if (smoothWindow <= 1) {{
        const milkLabel = milkMetricLabel(milkMetric).replace(/^./, (c) => c.toUpperCase());
        baseTitle = `${{milkLabel}} per day (mL)`;
      }} else {{
        const milkLabel = milkMetricLabel(milkMetric).replace(/^./, (c) => c.toUpperCase());
        baseTitle = `${{milkLabel}} per day (mL, ${{smoothWindow}}-day moving avg)`;
      }}
      if (!isTimelineMode(plotMode) && plotMode !== "milk-cumulative" && !isMilkMode(plotMode) && !isPercentMode(plotMode) && smoothWindow > 1) {{
        baseTitle = `${{baseTitle}} (${{smoothWindow}}-day moving avg)`;
      }}
      if (!splitEnabled || isTimelineMode(plotMode)) {{
        return baseTitle;
      }}
      return `${{baseTitle}} (Day/Night split, night ${{splitWindowText(nightStartHour)}})`;
    }}

    function smoothingText(plotMode, smoothWindow) {{
      if (!isSmoothable(plotMode)) {{
        return isTimelineMode(plotMode) ? "Smoothing disabled in Timeline mode" : "Smoothing disabled in cumulative mode";
      }}
      if (smoothWindow <= 1) {{
        return "Smoothing: off";
      }}
      return `Smoothing: ${{smoothWindow}}-day moving average`;
    }}

    function splitText(splitEnabled, nightStartHour) {{
      if (!splitEnabled) {{
        return "Split: off";
      }}
      return `Split: on (night ${{splitWindowText(nightStartHour)}})`;
    }}

    function tooltipTitle(items) {{
      if (!items || !items.length) {{
        return "";
      }}
      const idx = items[0].dataIndex;
      const chartLabels = (items[0].chart && items[0].chart.data && items[0].chart.data.labels) || activeLabels;
      const raw = items[0].raw || null;
      const dayKey = raw && raw.dayLabel ? raw.dayLabel : chartLabels[idx] || "";
      if (!dayKey) {{
        return "";
      }}
      const dayDate = new Date(`${{dayKey}}T00:00:00`);
      if (Number.isNaN(dayDate.getTime())) {{
        return dayKey;
      }}
      const weekday = dayDate.toLocaleDateString(undefined, {{ weekday: "long" }});
      return `${{dayKey}} (${{weekday}})`;
    }}

    function updateSmoothingLabel(mode, smoothWindow) {{
      const textEl = document.getElementById("smooth-window-value");
      if (!textEl) {{
        return;
      }}
      textEl.textContent = smoothingText(mode, smoothWindow);
    }}

    function updateSplitLabel(splitEnabled, nightStartHour) {{
      const textEl = document.getElementById("day-night-value");
      if (!textEl) {{
        return;
      }}
      textEl.textContent = splitText(splitEnabled, nightStartHour);
    }}

    function updateYAxisBounds(targetChart, plotMode) {{
      if (!targetChart || !targetChart.options || !targetChart.options.scales || !targetChart.options.scales.y) {{
        return;
      }}
      if (isTimelineMode(plotMode)) {{
        targetChart.options.scales.y.min = 0;
        targetChart.options.scales.y.max = 1440;
        targetChart.options.scales.y.reverse = true;
        targetChart.options.scales.y.ticks.callback = (value) => formatMinuteOfDay(value);
        return;
      }}
      if (isPercentMode(plotMode)) {{
        targetChart.options.scales.y.min = 0;
        targetChart.options.scales.y.max = 100;
        delete targetChart.options.scales.y.reverse;
        delete targetChart.options.scales.y.ticks.callback;
        return;
      }}
      delete targetChart.options.scales.y.min;
      delete targetChart.options.scales.y.max;
      delete targetChart.options.scales.y.reverse;
      delete targetChart.options.scales.y.ticks.callback;
    }}

    function updateXAxisBounds(targetChart, plotMode) {{
      if (!targetChart || !targetChart.options || !targetChart.options.scales || !targetChart.options.scales.x) {{
        return;
      }}
      const xScale = targetChart.options.scales.x;
      xScale.type = isTimelineMode(plotMode) ? "linear" : "category";
      if (isTimelineMode(plotMode)) {{
        xScale.min = 0;
        xScale.max = Math.max(1, timelineDays.length);
        xScale.ticks.stepSize = 1;
        xScale.ticks.precision = 0;
        return;
      }}
      delete xScale.min;
      delete xScale.max;
      delete xScale.ticks.stepSize;
      delete xScale.ticks.precision;
    }}

    function renderTimelineLegend(plotMode) {{
      const legendEl = document.getElementById("timeline-legend");
      if (!legendEl) {{
        return;
      }}
      legendEl.hidden = !isTimelineMode(plotMode);
      if (!isTimelineMode(plotMode)) {{
        return;
      }}
      legendEl.textContent = "";
      timelineVisibleSummary().presentTypeIndexes
        .forEach((typeIndex) => {{
        const type = timelineTypes[typeIndex] || {{}};
        const typeKey = type.key || "event";
        const isHidden = hiddenTimelineTypeKeys.has(typeKey);
        const item = document.createElement("button");
        item.type = "button";
        item.className = `timeline-legend-item${{isHidden ? " is-hidden" : ""}}`;
        item.setAttribute("aria-pressed", String(!isHidden));
        item.addEventListener("click", () => {{
          if (hiddenTimelineTypeKeys.has(typeKey)) {{
            hiddenTimelineTypeKeys.delete(typeKey);
          }} else {{
            hiddenTimelineTypeKeys.add(typeKey);
          }}
          refreshChart("none", {{ preserveXRange: true }});
        }});
        const emoji = document.createElement("span");
        emoji.className = "timeline-legend-emoji";
        const baseEmoji = document.createElement("span");
        baseEmoji.textContent = type.emoji || "•";
        emoji.appendChild(baseEmoji);
        const overlayText = timelineOverlayEmoji(typeKey);
        if (overlayText) {{
          const overlay = document.createElement("span");
          overlay.className = "timeline-legend-emoji-overlay";
          overlay.textContent = overlayText;
          emoji.appendChild(overlay);
        }}
        const label = document.createElement("span");
        label.textContent = type.label || "Event";
        item.appendChild(emoji);
        item.appendChild(label);
        legendEl.appendChild(item);
      }});
    }}

    function updateVisibleRange(chart) {{
      const textEl = document.getElementById("visible-range");
      const chartLabels = (chart && chart.data && chart.data.labels) || activeLabels;
      if (!textEl || !chartLabels.length) {{
        return;
      }}
      const xScale = chart.scales.x;
      const minIdx = Math.max(0, Math.ceil(xScale.min ?? 0));
      const maxIdx = Math.min(chartLabels.length - 1, Math.floor(xScale.max ?? chartLabels.length - 1));
      textEl.textContent = `Visible range: ${{chartLabels[minIdx]}} to ${{chartLabels[maxIdx]}}`;
      if (isTimelineMode(chart.$mode || "")) {{
        renderTimelineLegend(chart.$mode);
      }}
    }}

    function updateAfterXScaleChange(targetChart) {{
      if (targetChart && isTimelineMode(targetChart.$mode || "")) {{
        refreshChart("none", {{ preserveXRange: true }});
        return;
      }}
      updateVisibleRange(targetChart);
    }}

    function updateControlStates(plotMode, splitEnabled, modeSelect, smoothSlider, splitToggle, nightStartSelect, diaperMetricControls, diaperMetricSelect, milkMetricControls, milkMetricSelect) {{
      if (modeSelect) {{
        modeSelect.disabled = false;
      }}
      const timelineVisible = isTimelineMode(plotMode);
      if (smoothSlider) {{
        smoothSlider.disabled = !isSmoothable(plotMode);
      }}
      const splitAvailable = !timelineVisible;
      if (splitToggle) {{
        splitToggle.disabled = !splitAvailable;
        if (!splitAvailable) {{
          splitToggle.checked = false;
        }}
      }}
      if (nightStartSelect) {{
        nightStartSelect.disabled = !(splitAvailable && splitEnabled);
      }}
      const diaperControlsVisible = !timelineVisible && isDiaperMode(plotMode);
      if (diaperMetricControls) {{
        diaperMetricControls.hidden = !diaperControlsVisible;
      }}
      if (diaperMetricSelect) {{
        diaperMetricSelect.disabled = !diaperControlsVisible;
      }}
      const milkMetricControlsVisible = !timelineVisible && isMilkDailyMode(plotMode);
      if (milkMetricControls) {{
        milkMetricControls.hidden = !milkMetricControlsVisible;
      }}
      if (milkMetricSelect) {{
        milkMetricSelect.disabled = !milkMetricControlsVisible;
      }}
      const timelineLegend = document.getElementById("timeline-legend");
      if (timelineLegend) {{
        timelineLegend.hidden = !timelineVisible;
      }}
    }}

    function applyHiddenSeriesState(targetChart) {{
      if (!targetChart) {{
        return;
      }}
      targetChart.data.datasets.forEach((dataset, idx) => {{
        const key = dataset.customKey || dataset.label || String(idx);
        targetChart.setDatasetVisibility(idx, !isDatasetHidden(key, Boolean(dataset.defaultHidden)));
      }});
    }}

    const canvas = document.getElementById("milk-chart");
    const noData = document.getElementById("no-data");
    const chartWrap = document.querySelector(".chart-wrap");
    const modeSelect = document.getElementById("series-mode");
    const milkMetricControls = document.getElementById("milk-metric-controls");
    const milkMetricSelect = document.getElementById("milk-metric");
    const diaperMetricControls = document.getElementById("diaper-metric-controls");
    const diaperMetricSelect = document.getElementById("diaper-metric");
    const smoothSlider = document.getElementById("smooth-window");
    const splitToggle = document.getElementById("split-day-night");
    const nightStartSelect = document.getElementById("night-start-hour");

    function currentNightStart() {{
      const raw = Number.parseInt(nightStartSelect ? nightStartSelect.value : String(defaultNightStart), 10);
      if (Number.isNaN(raw)) {{
        return defaultNightStart;
      }}
      return Math.max(0, Math.min(23, raw));
    }}

    function currentSplitEnabled() {{
      return Boolean(splitToggle && splitToggle.checked);
    }}

    function currentDiaperMetric() {{
      if (!diaperMetricSelect) {{
        return "all";
      }}
      return diaperMetricSelect.value || "all";
    }}

    function currentMilkMetric() {{
      if (!milkMetricSelect) {{
        return "all";
      }}
      return milkMetricSelect.value || "all";
    }}

    function colorWithAlpha(color, alpha) {{
      const hexMatch = String(color || "").match(/^#([0-9a-f]{{6}})$/i);
      if (hexMatch) {{
        const value = Number.parseInt(hexMatch[1], 16);
        const red = (value >> 16) & 255;
        const green = (value >> 8) & 255;
        const blue = value & 255;
        return `rgba(${{red}},${{green}},${{blue}},${{alpha}})`;
      }}
      return color || `rgba(255,255,255,${{alpha}})`;
    }}

    const timelineBandsPlugin = {{
      id: "timelineBands",
      beforeDraw(targetChart) {{
        if (!isTimelineMode(targetChart.$mode || "")) {{
          return;
        }}
        const xScale = targetChart.scales && targetChart.scales.x;
        const chartArea = targetChart.chartArea;
        if (!xScale || !chartArea) {{
          return;
        }}
        const {{ visibleChildIndexes, bandByChild, separatorStart }} = timelineBandLayout();
        if (!visibleChildIndexes.length) {{
          return;
        }}
        const ctx = targetChart.ctx;
        const minDay = Math.max(0, Math.floor(xScale.min ?? 0));
        const maxDay = Math.min(timelineDays.length - 1, Math.ceil(xScale.max ?? timelineDays.length) - 1);
        ctx.save();
        ctx.beginPath();
        ctx.rect(chartArea.left, chartArea.top, chartArea.right - chartArea.left, chartArea.bottom - chartArea.top);
        ctx.clip();
        for (let dayIndex = minDay; dayIndex <= maxDay; dayIndex += 1) {{
          visibleChildIndexes.forEach((childIndex) => {{
            const child = timelineChildren[childIndex] || {{}};
            const band = bandByChild.get(childIndex);
            if (!band) {{
              return;
            }}
            const x1 = xScale.getPixelForValue(dayIndex + band.startOffset);
            const x2 = xScale.getPixelForValue(dayIndex + band.startOffset + band.bandWidth);
            ctx.fillStyle = colorWithAlpha(child.backgroundColor || child.borderColor || "#ffffff", child.key ? 0.3 : 0.18);
            ctx.fillRect(Math.min(x1, x2), chartArea.top, Math.abs(x2 - x1), chartArea.bottom - chartArea.top);
          }});
          const separatorX1 = xScale.getPixelForValue(dayIndex + separatorStart);
          const separatorX2 = xScale.getPixelForValue(dayIndex + 1);
          ctx.fillStyle = "rgba(0,0,0,0.65)";
          ctx.fillRect(Math.min(separatorX1, separatorX2), chartArea.top, Math.abs(separatorX2 - separatorX1), chartArea.bottom - chartArea.top);
        }}
        ctx.restore();
      }},
    }};

    function drawTimelineEmoji(ctx, point, x, y) {{
      ctx.fillText(point.emoji, x, y);
      if (!point.overlayEmoji) {{
        return;
      }}
      const baseFont = ctx.font;
      const overlaySize = Math.max(7, Math.floor(timelineEmojiFontSize() * 0.72));
      ctx.font = timelineEmojiFont(overlaySize);
      ctx.fillText(point.overlayEmoji, x + timelineEmojiFontSize() * 0.28, y - timelineEmojiFontSize() * 0.28);
      ctx.font = baseFont;
    }}

    const timelineEmojiPlugin = {{
      id: "timelineEmoji",
      afterDatasetsDraw(targetChart) {{
        if (!isTimelineMode(targetChart.$mode || "")) {{
          return;
        }}
        const ctx = targetChart.ctx;
        const chartArea = targetChart.chartArea;
        ctx.save();
        ctx.beginPath();
        ctx.rect(chartArea.left, chartArea.top, chartArea.right - chartArea.left, chartArea.bottom - chartArea.top);
        ctx.clip();
        ctx.font = timelineEmojiFont();
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        const drawItems = [];
        targetChart.data.datasets.forEach((dataset, datasetIndex) => {{
          if (!targetChart.isDatasetVisible(datasetIndex)) {{
            return;
          }}
          const meta = targetChart.getDatasetMeta(datasetIndex);
          meta.data.forEach((element, pointIndex) => {{
            const point = dataset.data[pointIndex];
            if (!point || !point.emoji || element.skip) {{
              return;
            }}
            drawItems.push({{ point, x: element.x, y: element.y + 0.5 }});
          }});
        }});
        drawItems
          .sort((a, b) => a.y - b.y || a.x - b.x)
          .forEach((item) => drawTimelineEmoji(ctx, item.point, item.x, item.y));
        ctx.restore();
      }},
    }};

    function labelsForMode(plotMode) {{
      return isTimelineMode(plotMode) ? timelineDays : labels;
    }}

    let chart = null;
    if ((!labels.length || !series.length) && (!timelineDays.length || !timelinePoints.length)) {{
      if (chartWrap) {{
        chartWrap.style.display = "none";
      }}
      if (noData) {{
        noData.hidden = false;
      }}
      if (modeSelect) {{
        modeSelect.disabled = true;
      }}
      if (smoothSlider) {{
        smoothSlider.disabled = true;
      }}
      if (splitToggle) {{
        splitToggle.disabled = true;
      }}
      if (nightStartSelect) {{
        nightStartSelect.disabled = true;
      }}
      if (diaperMetricSelect) {{
        diaperMetricSelect.disabled = true;
      }}
      if (diaperMetricControls) {{
        diaperMetricControls.hidden = true;
      }}
      if (milkMetricSelect) {{
        milkMetricSelect.disabled = true;
      }}
      if (milkMetricControls) {{
        milkMetricControls.hidden = true;
      }}
      updateSmoothingLabel("milk-daily", 1);
      updateSplitLabel(false, defaultNightStart);
    }} else {{
      const initialMode = labels.length && series.length ? "milk-daily" : "timeline";
      const initialDiaperMetric = "all";
      const initialMilkMetric = "all";
      const initialSmoothWindow = 1;
      const initialSplitEnabled = false;
      const initialNightStart = defaultNightStart;
      activeLabels = labelsForMode(initialMode);
      chart = new Chart(canvas, {{
        type: "line",
        data: {{
          labels: activeLabels,
          datasets: buildDatasets(initialMode, initialDiaperMetric, initialMilkMetric, initialSmoothWindow, initialSplitEnabled, initialNightStart),
        }},
        plugins: [timelineBandsPlugin, timelineEmojiPlugin],
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          interaction: {{
            mode: "nearest",
            intersect: false,
          }},
          plugins: {{
            legend: {{
              labels: {{
                color: "#f2f2f2",
                generateLabels: (targetChart) => {{
                  const defaultGenerator = Chart.defaults &&
                    Chart.defaults.plugins &&
                    Chart.defaults.plugins.legend &&
                    Chart.defaults.plugins.legend.labels &&
                    Chart.defaults.plugins.legend.labels.generateLabels;
                  const labels = defaultGenerator
                    ? defaultGenerator(targetChart)
                    : targetChart.data.datasets.map((dataset, datasetIndex) => ({{
                      text: dataset.label,
                      datasetIndex,
                      hidden: !targetChart.isDatasetVisible(datasetIndex),
                      fillStyle: dataset.backgroundColor,
                      strokeStyle: dataset.borderColor,
                    }}));
                  if (!isTimelineMode(targetChart.$mode || "")) {{
                    return labels;
                  }}
                  return labels.map((item) => {{
                    const dataset = targetChart.data.datasets[item.datasetIndex];
                    if (!dataset) {{
                      return item;
                    }}
                    return {{
                      ...item,
                      fillStyle: dataset.backgroundColor,
                      strokeStyle: dataset.borderColor,
                      lineWidth: 2,
                    }};
                  }});
                }},
              }},
              onClick: (event, legendItem, legend) => {{
                const targetChart = legend.chart;
                const idx = legendItem.datasetIndex;
                if (idx == null) {{
                  return;
                }}
                const dataset = targetChart.data.datasets[idx];
                const key = (dataset && (dataset.customKey || dataset.label)) || String(idx);
                const currentlyVisible = targetChart.isDatasetVisible(idx);
                if (currentlyVisible) {{
                  hiddenSeriesKeys.add(key);
                  shownSeriesKeys.delete(key);
                }} else {{
                  hiddenSeriesKeys.delete(key);
                  shownSeriesKeys.add(key);
                }}
                if (isTimelineMode(targetChart.$mode || "")) {{
                  refreshChart("none", {{ preserveXRange: true }});
                  return;
                }}
                targetChart.setDatasetVisibility(idx, !currentlyVisible);
                targetChart.update();
              }},
            }},
            tooltip: {{
              callbacks: {{
                title: (items) => tooltipTitle(items),
                label: (context) => {{
                  const plotMode = context.chart.$mode || "milk-daily";
                  if (isTimelineMode(plotMode)) {{
                    const raw = context.raw || {{}};
                    const lines = [];
                    const childLabel = context.dataset.label || "";
                    const startTime = formatMinuteOfDay(raw.y);
                    const endTime = raw.endMinute == null ? "" : formatMinuteOfDay(raw.endMinute);
                    lines.push(`${{raw.emoji || "•"}} ${{raw.typeLabel || "Event"}}${{childLabel ? ` · ${{childLabel}}` : ""}}`);
                    lines.push(endTime ? `${{startTime}} to ${{endTime}}` : startTime);
                    const valueText = timelineValueText(raw);
                    if (valueText) {{
                      lines.push(valueText);
                    }}
                    if (raw.note) {{
                      lines.push(raw.note);
                    }}
                    return lines;
                  }}
                  const windowSize = context.chart.$smoothWindow || 1;
                  const smoothSuffix = isSmoothable(plotMode) && windowSize > 1 ? `, ${{windowSize}}d MA` : "";
                  const splitSuffix = context.chart.$splitEnabled ? ", split" : "";
                  const unit = plotUnit(plotMode);
                  const decimals = plotValueDecimals(plotMode, windowSize);
                  if (isPercentMode(plotMode)) {{
                    const breastPercent = context.parsed.y;
                    const formulaPercent = Number((100 - breastPercent).toFixed(decimals));
                    return `${{context.dataset.label}}: ${{breastPercent.toFixed(decimals)}}% breast milk, ${{formulaPercent.toFixed(decimals)}}% formula${{smoothSuffix}}${{splitSuffix}}`;
                  }}
                  const diaperMetric = context.chart.$diaperMetric || "all";
                  const milkMetric = context.chart.$milkMetric || "all";
                  const mainLine = `${{context.dataset.label}}: ${{context.parsed.y.toFixed(decimals)}} ${{unit}} (${{plotModeLabel(plotMode, diaperMetric, milkMetric)}}${{smoothSuffix}}${{splitSuffix}})`;
                  if (isDiaperMode(plotMode)) {{
                    const diaperMeta = context.dataset.diaperMeta || [];
                    const meta = diaperMeta[context.dataIndex] || null;
                    if (meta) {{
                      const detailLines = [mainLine];
                      if (windowSize > 1) {{
                        detailLines.push(`raw matches: ${{meta.count}}`);
                      }}
                      if (meta.blowoutCount > 0) {{
                        detailLines.push(`blowouts: ${{meta.blowoutCount}}`);
                        if (Array.isArray(meta.blowoutDetails) && meta.blowoutDetails.length) {{
                          detailLines.push(`notes: ${{meta.blowoutDetails.join("; ")}}`);
                        }}
                      }}
                      return detailLines;
                    }}
                  }}
                  if (plotMode === "gap-max" && windowSize <= 1) {{
                    const periods = context.dataset.maxGapPeriods || [];
                    const periodText = periods[context.dataIndex] || "";
                    if (periodText) {{
                      const parts = periodText.split(" to ");
                      if (parts.length >= 2) {{
                        return [mainLine, `start: ${{parts[0]}}`, `end: ${{parts.slice(1).join(" to ")}}`];
                      }}
                      return [mainLine, `gap: ${{periodText}}`];
                    }}
                  }}
                  return mainLine;
                }},
              }},
            }},
            zoom: {{
              pan: {{
                enabled: true,
                mode: "x",
                modifierKey: "shift",
              }},
              zoom: {{
                mode: "x",
                wheel: {{ enabled: true }},
                pinch: {{ enabled: true }},
                drag: {{
                  enabled: true,
                  backgroundColor: "rgba(30, 136, 229, 0.2)",
                  borderColor: "rgba(30, 136, 229, 0.9)",
                  borderWidth: 1,
                }},
                onZoomComplete: ({{ chart }}) => updateAfterXScaleChange(chart),
              }},
              onPanComplete: ({{ chart }}) => updateAfterXScaleChange(chart),
            }},
          }},
          scales: {{
             x: {{
               type: isTimelineMode(initialMode) ? "linear" : "category",
               min: isTimelineMode(initialMode) ? 0 : undefined,
               max: isTimelineMode(initialMode) ? Math.max(1, timelineDays.length) : undefined,
               ticks: {{
                  color: "#d2d2d2",
                  autoSkip: false,
                  stepSize: isTimelineMode(initialMode) ? 1 : undefined,
                  precision: isTimelineMode(initialMode) ? 0 : undefined,
                   maxRotation: 0,
                  callback: function(value, index) {{
                    return axisLabelForTickValue(value, index, this);
                  }},
                }},
                grid: {{
                  color: (context) => isWeeklyTickValue(context.tick && context.tick.value, context.index) ? "rgba(255,255,255,0.12)" : "rgba(255,255,255,0)",
                  lineWidth: (context) => isWeeklyTickValue(context.tick && context.tick.value, context.index) ? 1 : 0,
                }},
               title: {{ display: true, color: "#d2d2d2", text: "Day" }},
             }},
            y: {{
              ticks: {{ color: "#d2d2d2" }},
              grid: {{ color: "rgba(255,255,255,0.08)" }},
               title: {{ display: true, color: "#d2d2d2", text: yAxisTitle(initialMode, initialDiaperMetric, initialMilkMetric, initialSmoothWindow, initialSplitEnabled, initialNightStart) }},
            }},
          }},
        }},
      }});
      chart.$mode = initialMode;
      chart.$diaperMetric = initialDiaperMetric;
      chart.$milkMetric = initialMilkMetric;
      chart.$smoothWindow = initialSmoothWindow;
      chart.$splitEnabled = initialSplitEnabled;
      chart.$nightStart = initialNightStart;
      updateXAxisBounds(chart, initialMode);
      updateYAxisBounds(chart, initialMode);
      if (modeSelect) {{
        modeSelect.value = initialMode;
      }}
      if (smoothSlider) {{
        smoothSlider.value = String(initialSmoothWindow);
      }}
      if (diaperMetricSelect) {{
        diaperMetricSelect.value = initialDiaperMetric;
      }}
      if (milkMetricSelect) {{
        milkMetricSelect.value = initialMilkMetric;
      }}
      if (splitToggle) {{
        splitToggle.checked = initialSplitEnabled;
      }}
      if (nightStartSelect) {{
        nightStartSelect.value = String(initialNightStart);
      }}
      updateControlStates(initialMode, initialSplitEnabled, modeSelect, smoothSlider, splitToggle, nightStartSelect, diaperMetricControls, diaperMetricSelect, milkMetricControls, milkMetricSelect);
      updateSmoothingLabel(initialMode, initialSmoothWindow);
      updateSplitLabel(initialSplitEnabled, initialNightStart);
      renderTimelineLegend(initialMode);
      updateVisibleRange(chart);
    }}

    function refreshChart(animationMode, options = {{}}) {{
      if (!chart) {{
        return;
      }}
      const mode = chart.$mode || "milk-daily";
      const diaperMetric = chart.$diaperMetric || currentDiaperMetric();
      const milkMetric = chart.$milkMetric || currentMilkMetric();
      const smoothWindow = chart.$smoothWindow || 1;
      const splitEnabled = currentSplitEnabled();
      const nightStart = currentNightStart();
      chart.$diaperMetric = diaperMetric;
      chart.$milkMetric = milkMetric;
      chart.$splitEnabled = isTimelineMode(mode) ? false : splitEnabled;
      chart.$nightStart = nightStart;
      activeLabels = labelsForMode(mode);
      chart.data.labels = activeLabels;
      const preservedXRange = options.preserveXRange && chart.scales && chart.scales.x
        ? {{ min: chart.scales.x.min, max: chart.scales.x.max }}
        : null;
      updateXAxisBounds(chart, mode);
      if (preservedXRange && Number.isFinite(preservedXRange.min) && Number.isFinite(preservedXRange.max)) {{
        chart.options.scales.x.min = preservedXRange.min;
        chart.options.scales.x.max = preservedXRange.max;
      }}
      updateControlStates(mode, chart.$splitEnabled, modeSelect, smoothSlider, splitToggle, nightStartSelect, diaperMetricControls, diaperMetricSelect, milkMetricControls, milkMetricSelect);
      chart.data.datasets = buildDatasets(mode, diaperMetric, milkMetric, smoothWindow, chart.$splitEnabled, nightStart);
      applyHiddenSeriesState(chart);
      chart.options.scales.y.title.text = yAxisTitle(mode, diaperMetric, milkMetric, smoothWindow, chart.$splitEnabled, nightStart);
      updateYAxisBounds(chart, mode);
      chart.update(animationMode);
      updateSmoothingLabel(mode, smoothWindow);
      updateSplitLabel(chart.$splitEnabled, nightStart);
      renderTimelineLegend(mode);
      updateVisibleRange(chart);
    }}

    if (modeSelect) {{
      modeSelect.addEventListener("change", (event) => {{
        if (!chart) {{
          return;
        }}
        chart.$mode = event.target.value || "milk-daily";
        refreshChart();
      }});
    }}

    if (diaperMetricSelect) {{
      diaperMetricSelect.addEventListener("change", (event) => {{
        if (!chart) {{
          return;
        }}
        chart.$diaperMetric = event.target.value || "all";
        refreshChart("none");
      }});
    }}

    if (milkMetricSelect) {{
      milkMetricSelect.addEventListener("change", (event) => {{
        if (!chart) {{
          return;
        }}
        chart.$milkMetric = event.target.value || "all";
        refreshChart("none");
      }});
    }}

    if (smoothSlider) {{
      smoothSlider.addEventListener("input", (event) => {{
        const nextWindow = Math.max(1, Number.parseInt(event.target.value, 10) || 1);
        if (!chart) {{
          updateSmoothingLabel("milk-daily", nextWindow);
          return;
        }}
        chart.$smoothWindow = nextWindow;
        refreshChart("none");
      }});
    }}

    if (splitToggle) {{
      splitToggle.addEventListener("change", () => {{
        refreshChart("none");
      }});
    }}

    if (nightStartSelect) {{
      nightStartSelect.addEventListener("change", () => {{
        refreshChart("none");
      }});
    }}

    document.getElementById("reset-zoom").addEventListener("click", () => {{
      if (!chart) {{
        return;
      }}
      chart.resetZoom();
      updateAfterXScaleChange(chart);
    }});
    """.strip()

    warn_html = ""
    if chart_data.get("skippedUnits"):
        warn_html = (
            f"<div class=\"warn\">Skipped {int(chart_data['skippedUnits'])} feed entries with unsupported units.</div>"
        )

    return f"""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Nara Plots</title>
  <link rel=\"icon\" href=\"/favicon.svg\" type=\"image/svg+xml\" />
  <style>
    {css}
  </style>
</head>
<body>
  <div class=\"container\">
    <h1>Plots</h1>
    <p class=\"subtitle\">Choose a plot (milk totals, diaper counts, feeding gaps, or timeline). Day/Night split uses a 12-hour night window from the selected start time (default 20:00). Drag horizontally to zoom; hold Shift and drag to pan. On diaper plots, dark highlighted dots mark days with at least one blowout.</p>
    <div class=\"actions\">
      <a class=\"btn\" href=\"/\">Back to Main View</a>
      <select id=\"series-mode\" class=\"mode-select\" aria-label=\"Series mode\">
        <option value=\"milk-daily\" selected>Daily Milk Total</option>
        <option value=\"milk-cumulative\">Cumulative Milk Total</option>
        <option value=\"milk-average-feed\">Average Milk Per Feed</option>
        <option value=\"milk-max-feed\">Max Milk Per Feed</option>
        <option value=\"milk-breast-percent\">Breast Milk vs Formula %</option>
        <option value=\"diaper-daily\">Daily Diaper Changes</option>
        <option value=\"gap-max\">Max Feeding Gap</option>
        <option value=\"gap-avg\">Average Feeding Gap</option>
        <option value=\"timeline\">Timeline</option>
      </select>
      <label id=\"milk-metric-controls\" class=\"subtitle\">
        Milk type
        <select id=\"milk-metric\" class=\"mode-select\" aria-label=\"Milk type\">
          <option value=\"all\" selected>All</option>
          <option value=\"breast\">Breast</option>
          <option value=\"formula\">Formula</option>
        </select>
      </label>
      <label id=\"diaper-metric-controls\" class=\"subtitle\" hidden>
        Diaper type
        <select id=\"diaper-metric\" class=\"mode-select\" aria-label=\"Diaper type\">
          <option value=\"all\" selected>All</option>
          <option value=\"dirty\">Dirty</option>
          <option value=\"wet\">Wet</option>
          <option value=\"dry\">Dry</option>
        </select>
      </label>
      <label class=\"toggle-label\" for=\"split-day-night\">
        <input id=\"split-day-night\" type=\"checkbox\" />
        Day/Night Split
      </label>
      <label for=\"night-start-hour\" class=\"subtitle\">Night starts</label>
      <select id=\"night-start-hour\" class=\"mode-select\" aria-label=\"Night start hour\">
        {night_start_options_html}
      </select>
      <span id=\"day-night-value\" class=\"subtitle\">Split: off</span>
      <div class=\"smoothing\">
        <label for=\"smooth-window\" class=\"subtitle\">Smoothing</label>
        <input id=\"smooth-window\" class=\"smooth-slider\" type=\"range\" min=\"1\" max=\"21\" step=\"1\" value=\"1\" />
        <span id=\"smooth-window-value\" class=\"subtitle\">Smoothing: off</span>
      </div>
      <button id=\"reset-zoom\" class=\"btn\" type=\"button\">Reset Zoom</button>
      <span id=\"visible-range\" class=\"subtitle\"></span>
    </div>
    <div id=\"timeline-legend\" class=\"timeline-legend\" hidden></div>
    <div class=\"chart-wrap\">
      <canvas id=\"milk-chart\"></canvas>
    </div>
    <div id=\"no-data\" class=\"subtitle\" hidden>No plot data found yet.</div>
    <div class=\"meta\">as of {html.escape(generated)}</div>
    {warn_html}
  </div>
  <script src=\"https://cdn.jsdelivr.net/npm/chart.js@4.4.8/dist/chart.umd.min.js\"></script>
  <script src=\"https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js\"></script>
  <script src=\"https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.2.0/dist/chartjs-plugin-zoom.min.js\"></script>
  <script>
    {script}
  </script>
</body>
</html>
"""


class NaraServer(ThreadingHTTPServer):
    daemon_threads = True

    adb_path: str
    adb_device: Optional[str]
    nara_db_path: Path
    firebase_db_path: Path
    cache_ttl: float
    cache_data: Optional[Dict[str, Any]]
    cache_time: float
    cache_lock: threading.Lock
    auth_failures: Dict[str, Dict[str, Any]]
    password_hash: Optional[str]


def fetch_live_data(server):
    now = time.time()
    cache_data = getattr(server, "cache_data", None)
    cache_time = getattr(server, "cache_time", 0.0)
    cache_ttl = getattr(server, "cache_ttl", 0.0)
    if cache_data is not None and cache_ttl > 0 and (now - cache_time) < cache_ttl:
        return cache_data, False

    cache_lock = getattr(server, "cache_lock", None)
    if cache_lock is None:
        cache_lock = threading.Lock()
        server.cache_lock = cache_lock

    acquired = cache_lock.acquire(blocking=cache_data is None)
    if not acquired:
        return cache_data, True

    try:
        now = time.time()
        cache_data = getattr(server, "cache_data", None)
        cache_time = getattr(server, "cache_time", 0.0)
        cache_ttl = getattr(server, "cache_ttl", 0.0)
        if cache_data is not None and cache_ttl > 0 and (now - cache_time) < cache_ttl:
            return cache_data, False

        adb_pull(server.adb_path, REMOTE_NARA_DB, server.nara_db_path, server.adb_device)
        adb_pull(server.adb_path, REMOTE_FIREBASE_DB, server.firebase_db_path, server.adb_device)
        data = collect_live_data(server.nara_db_path, server.firebase_db_path)
        server.cache_data = data
        server.cache_time = time.time()
        return data, False
    finally:
        cache_lock.release()


class Handler(BaseHTTPRequestHandler):
    def send_unauthorized(self, html_page=False):
        if html_page:
            body_bytes = build_auth_html(self.path or "/").encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", cleared_auth_cookie_header())
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
            return

        body_bytes = b"Unauthorized"
        self.send_response(401)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", cleared_auth_cookie_header())
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def send_rate_limited(self, wait_seconds):
        retry_after = format_retry_after_seconds(wait_seconds)
        body_bytes = (
            f"Too many login attempts. Try again in {retry_after} second"
            f"{'s' if retry_after != 1 else ''}."
        ).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Retry-After", str(retry_after))
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/auth":
            self.send_response(404)
            self.end_headers()
            return

        server = cast(NaraServer, self.server)
        client_key = client_address_text(self)
        wait_seconds = auth_throttle_wait_seconds(server, client_key)
        if wait_seconds > 0:
            self.send_rate_limited(wait_seconds)
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(content_length) if content_length > 0 else b""
        params = parse_qs(raw_body.decode("utf-8", errors="replace"))
        password = params.get("password", [""])[0]

        if not server.password_hash:
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", cleared_auth_cookie_header())
            self.end_headers()
            return

        auth_status, wait_seconds = authenticate_password_attempt(
            server,
            client_key,
            password,
            parsed.path,
        )
        if auth_status == "rate_limited":
            self.send_rate_limited(wait_seconds)
            return

        if auth_status == "authorized":
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", auth_cookie_header(server.password_hash))
            self.end_headers()
            return

        self.send_unauthorized(html_page=False)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/favicon.svg":
            icon_path = Path(__file__).resolve().parent / "favicon.svg"
            if not icon_path.exists():
                self.send_response(404)
                self.end_headers()
                return
            data = icon_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path in ("/milk", "/milk.html"):
            self.send_response(302)
            self.send_header("Location", "/plot")
            self.end_headers()
            return
        if parsed.path not in ("/", "/index.html", "/json", "/plot", "/plot.html"):
            self.send_response(404)
            self.end_headers()
            return
        auth_status, wait_seconds = request_auth_status(self)
        if auth_status == "rate_limited":
            self.send_rate_limited(wait_seconds)
            return
        if auth_status != "authorized":
            self.send_unauthorized(html_page=parsed.path != "/json")
            return

        try:
            server = cast(NaraServer, self.server)
            data, _is_stale = fetch_live_data(server)
            latest_feed = latest_by_group(data.get("events", []), "FEED")
            latest_diaper = latest_by_group(data.get("events", []), "DIAPER")
            latest_poopy = latest_poopy_diapers(data.get("events", []))
            generated_at = data.get("generatedAt", int(time.time() * 1000))
            vitamins = routine_counts_today(data.get("events", []), ["vitamin"])
            medications = routine_counts_today(data.get("events", []), ["medication", "medicine"])
            baths = routine_counts_today(data.get("events", []), ["bath"])
            if parsed.path == "/json":
                payload = build_json(
                    latest_feed,
                    latest_diaper,
                    latest_poopy,
                    data.get("children", {}),
                    generated_at,
                    vitamins,
                    medications,
                    baths,
                )
                body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return

            if parsed.path in ("/plot", "/plot.html"):
                html_body = build_plot_html(
                    data.get("events", []),
                    data.get("children", {}),
                    generated_at,
                )
                body_bytes = html_body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return

            params = parse_qs(parsed.query)
            side = params.get("side", [""])[0]
            body_class = "bottom" if side == "bottom" else ""
            html_body = build_html(
                latest_feed,
                latest_diaper,
                latest_poopy,
                data.get("children", {}),
                generated_at,
                body_class,
                vitamins,
                medications,
                baths,
            )
            body_bytes = html_body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return
        except Exception as exc:
            logging.exception("Request failed for %s", self.path)
            msg = f"Error: {exc}".encode("utf-8")
            try:
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                return


def main():
    base_path = Path(__file__).resolve().parent
    load_env_file(base_path / ENV_FILE_NAME)

    parser = argparse.ArgumentParser()
    parser.add_argument("--adb-path", dest="adb_path", default=os.environ.get("ADB_PATH", "adb"))
    parser.add_argument(
        "--adb-device",
        dest="adb_device",
        default=os.environ.get("ADB_DEVICE") or os.environ.get("ANDROID_SERIAL"),
    )
    parser.add_argument("--host", dest="host", default=os.environ.get("NARA_HOST") or "127.0.0.1")
    parser.add_argument("--port", dest="port", type=int, default=int(os.environ.get("NARA_PORT") or "8787"))
    args = parser.parse_args()

    server_password = os.environ.get("NARA_PASSWORD")

    base_dir = Path(__file__).resolve().parent.relative_to(os.getcwd())
    db_dir = base_dir / "nara_device_db"
    db_dir.mkdir(exist_ok=True)

    nara_db_path = db_dir / "nara.db"
    firebase_db_path = db_dir / "amazing-ripple-221320.firebaseio.com_default"

    server = NaraServer((args.host, args.port), Handler)
    server.adb_path = args.adb_path
    server.adb_device = args.adb_device
    server.nara_db_path = nara_db_path
    server.firebase_db_path = firebase_db_path
    server.cache_ttl = float(os.environ.get("NARA_CACHE_TTL", "10"))
    server.cache_data = None
    server.cache_time = 0.0
    server.cache_lock = threading.Lock()
    server.auth_failures = {}
    server.password_hash = password_digest(server_password) if server_password else None

    print(f"Serving on http://{args.host}:{args.port}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server.serve_forever()


if __name__ == "__main__":
    main()
