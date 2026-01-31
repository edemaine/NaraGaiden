# usage: python nara_web.py --host 0.0.0.0 --port 8888 --adb-device emulator-5554

import argparse
import html
import json
import logging
import os
import time
from typing import Any, Dict, Optional, cast
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from nara_live_export import (
    REMOTE_FIREBASE_DB,
    REMOTE_NARA_DB,
    adb_pull,
    collect_live_data,
)


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


def feed_label(ev):
    t = ev.get("trackTypeKey") or "FEED"
    payload = ev.get("payload") or {}
    if t == "FEED.BOTTLE":
        vol, unit = bottle_volume(payload)
        if vol is not None and unit:
            return f"Bottle ({format_amount(vol)} {unit})"
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


def build_body(latest_feed, latest_diaper, child_map, generated_at):
    now_ms = int(time.time() * 1000)
    rows = []
    child_keys = sorted(
        ## Skip babies with no latest feed (dogs):
        latest_feed.keys(),
        ## All babies:
        #set(latest_feed.keys()) | set(latest_diaper.keys()),
        key=lambda key: (child_map.get(key) or key),
    )
    for child_key in child_keys:
        name = child_map.get(child_key) or child_key
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
            f"<td class=\"group\">{html.escape(name)}</td>"
            f"<td class=\"group\">{html.escape(feed_text)}</td>"
            f"<td class=\"time\" style=\"background:{feed_bg}; color:{feed_fg};\">{html.escape(feed_when)}</td>"
            f"<td class=\"group\">{html.escape(diaper_text)}</td>"
            f"<td class=\"time\" style=\"background:{diaper_bg}; color:{diaper_fg};\">{html.escape(diaper_when)}</td>"
            "</tr>"
        )

    generated = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(generated_at / 1000))
    rows_html = "\n".join(rows) or "<tr><td colspan=\"5\">No feeds found</td></tr>"
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
    <div class=\"actions\">
      <button class=\"btn\" onclick=\"openCleanWindow()\">Open Window</button>
      <div class=\"meta\">Updated: {html.escape(generated)}</div>
    </div>
    """.strip()


def build_html(latest_feed, latest_diaper, child_map, generated_at, body_class=""):
    body_html = build_body(latest_feed, latest_diaper, child_map, generated_at)
    css = """
    @import url("https://fonts.googleapis.com/css2?family=Mystery+Quest&family=Slackey&display=swap");
    @view-transition { navigation: auto; }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: #0b0b0b;
      color: #f2f2f2;
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
      font-family: "Mystery Quest", "Noto Sans", cursive;
    }
    .meta {
      color: #a3a3a3;
      font-size: clamp(12px, 1vw + 6px, 16px);
      white-space: nowrap;
    }
    .actions {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: clamp(8px, 1.2vw, 16px);
    }
    .btn {
      appearance: none;
      border: 1px solid #2a2a2a;
      background: #141414;
      color: #f2f2f2;
      padding: 8px 12px;
      font-size: clamp(12px, 1vw + 6px, 16px);
      border-radius: 6px;
      cursor: pointer;
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
      font-family: "Slackey", "Mystery Quest", cursive;
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
    """.strip()
    script = """
    let lastSuccessMs = Date.now();
    let staleCount = 0;

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
      if (staleCount <= 0) {
        meta.textContent = meta.dataset.base;
        return;
      }
      const minutes = Math.max(1, Math.floor((Date.now() - lastSuccessMs) / 60000));
      const suffix = minutes === 1 ? "1 minute old" : `${minutes} minutes old`;
      meta.textContent = `${meta.dataset.base} (${suffix})`;
    }

    async function refreshContent() {
      try {
        const url = new URL(window.location.href);
        url.searchParams.set("_", Date.now().toString());
        const response = await fetch(url, { cache: "no-store" });
        if (!response.ok) {
          staleCount += 1;
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
          staleCount = 0;
          updateStaleNote();
        } else {
          staleCount += 1;
          updateStaleNote();
          console.warn("Refresh failed: missing container");
        }
      } catch (err) {
        staleCount += 1;
        updateStaleNote();
        console.warn("Refresh error", err);
      }
    }

    setInterval(refreshContent, 60000);
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




def build_json(latest_feed, latest_diaper, child_map, generated_at):
    now_ms = int(time.time() * 1000)
    child_keys = sorted(
        latest_feed.keys(),
        key=lambda key: (child_map.get(key) or key),
    )
    children = []
    for child_key in child_keys:
        name = child_map.get(child_key) or child_key
        feed_ev = latest_feed.get(child_key)
        diaper_ev = latest_diaper.get(child_key)
        feed_when = format_relative(feed_ev.get("beginDt"), now_ms) if feed_ev else "unknown"
        diaper_when = format_relative(diaper_ev.get("beginDt"), now_ms) if diaper_ev else "unknown"
        children.append(
            {
                "id": child_key,
                "name": name,
                "feed": {
                    "label": feed_label(feed_ev) if feed_ev else "unknown",
                    "beginDt": feed_ev.get("beginDt") if feed_ev else None,
                    "when": feed_when,
                },
                "diaper": {
                    "label": diaper_label(diaper_ev) if diaper_ev else "unknown",
                    "beginDt": diaper_ev.get("beginDt") if diaper_ev else None,
                    "when": diaper_when,
                },
            }
        )
    return {
        "generatedAt": generated_at,
        "children": children,
    }


class NaraServer(HTTPServer):
    adb_path: str
    adb_device: Optional[str]
    nara_db_path: Path
    firebase_db_path: Path
    cache_ttl: float
    cache_data: Optional[Dict[str, Any]]
    cache_time: float


def fetch_live_data(server):
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
    server.cache_time = now
    return data, False


class Handler(BaseHTTPRequestHandler):
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
        if parsed.path not in ("/", "/index.html", "/json"):
            self.send_response(404)
            self.end_headers()
            return

        try:
            server = cast(NaraServer, self.server)
            data, is_stale = fetch_live_data(server)
            latest_feed = latest_by_group(data.get("events", []), "FEED")
            latest_diaper = latest_by_group(data.get("events", []), "DIAPER")
            generated_at = data.get("generatedAt", int(time.time() * 1000))
            if parsed.path == "/json":
                payload = build_json(
                    latest_feed,
                    latest_diaper,
                    data.get("children", {}),
                    generated_at,
                )
                body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
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
                data.get("children", {}),
                generated_at,
                body_class,
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb-path", dest="adb_path", default=os.environ.get("ADB_PATH", "adb"))
    parser.add_argument(
        "--adb-device",
        dest="adb_device",
        default=os.environ.get("ADB_DEVICE") or os.environ.get("ANDROID_SERIAL"),
    )
    parser.add_argument("--host", dest="host", default="127.0.0.1")
    parser.add_argument("--port", dest="port", type=int, default=8787)
    args = parser.parse_args()

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

    print(f"Serving on http://{args.host}:{args.port}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server.serve_forever()


if __name__ == "__main__":
    main()
