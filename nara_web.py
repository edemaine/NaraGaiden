# usage: python nara_web.py --host 0.0.0.0 --port 8888 --adb-device emulator-5554

import argparse
from datetime import date, timedelta
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
      font-size: clamp(12px, 1vw + 6px, 18px);
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
    let lastSuccessMs = Date.now();
    let staleActive = false;

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
      if (!staleActive) {
        meta.textContent = meta.dataset.base;
        return;
      }
      const minutes = Math.max(0, Math.floor((Date.now() - lastSuccessMs) / 60000));
      if (minutes === 0) {
        meta.textContent = meta.dataset.base;
        return;
      }
      const suffix = minutes === 1 ? "1 min old" : `${minutes} mins old`;
      meta.textContent = `${meta.dataset.base} (${suffix})`;
    }

    async function refreshContent() {
      try {
        const url = new URL(window.location.href);
        url.searchParams.set("_", Date.now().toString());
        const response = await fetch(url, { cache: "no-store" });
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
    normalized = str(unit or "ML").strip().upper()
    if normalized in {"ML", "MILLILITER", "MILLILITERS"}:
        return float(volume)
    if normalized in {"L", "LITER", "LITERS"}:
        return float(volume) * 1000.0
    if normalized in {"OZ", "FL OZ", "FLOZ", "FL_OZ"}:
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


def milk_totals_by_day(events, child_map):
    by_child_day = {}
    by_child_day_hour = {}
    feed_counts_by_child_day = {}
    feed_counts_by_child_day_hour = {}
    diaper_counts_by_child_day = {}
    diaper_counts_by_child_day_hour = {}
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
            child_diaper_counts = diaper_counts_by_child_day.setdefault(child_key, {})
            child_diaper_counts[day_key] = child_diaper_counts.get(day_key, 0) + 1
            child_diaper_hour_counts = diaper_counts_by_child_day_hour.setdefault(child_key, {})
            day_hour_counts = child_diaper_hour_counts.setdefault(day_key, {})
            day_hour_counts[hour] = day_hour_counts.get(hour, 0) + 1
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

    all_babies_split = {}
    for night_start_hour in range(24):
        day_gap_max_points = []
        day_gap_max_periods = []
        day_gap_avg_points = []
        night_gap_max_points = []
        night_gap_max_periods = []
        night_gap_avg_points = []
        for day_key in labels:
            gap_hour_stats = gap_stats_all_day_hour.get(day_key, {})
            day_gap_sum = 0.0
            day_gap_count = 0
            day_gap_max = None
            day_gap_period = None
            night_gap_sum = 0.0
            night_gap_count = 0
            night_gap_max = None
            night_gap_period = None
            for hour, stat in gap_hour_stats.items():
                gap_sum = float(stat.get("sum", 0.0))
                gap_count = int(stat.get("count", 0))
                gap_max = float(stat.get("max", 0.0))
                if gap_count <= 0:
                    continue
                if is_night_hour(hour, night_start_hour):
                    night_gap_sum += gap_sum
                    night_gap_count += gap_count
                    if night_gap_max is None or gap_max > night_gap_max:
                        night_gap_max = gap_max
                        night_gap_period = _format_gap_period(
                            stat.get("maxStart"), stat.get("maxEnd")
                        )
                else:
                    day_gap_sum += gap_sum
                    day_gap_count += gap_count
                    if day_gap_max is None or gap_max > day_gap_max:
                        day_gap_max = gap_max
                        day_gap_period = _format_gap_period(
                            stat.get("maxStart"), stat.get("maxEnd")
                        )

            day_gap_max_points.append(day_gap_max)
            day_gap_max_periods.append(day_gap_period)
            day_gap_avg_points.append(day_gap_sum / day_gap_count if day_gap_count > 0 else None)
            night_gap_max_points.append(night_gap_max)
            night_gap_max_periods.append(night_gap_period)
            night_gap_avg_points.append(night_gap_sum / night_gap_count if night_gap_count > 0 else None)

        day_gap_max_display = _trim_optional_series(day_gap_max_points, decimals=2)
        day_gap_avg_display = _trim_optional_series(day_gap_avg_points, decimals=2)
        night_gap_max_display = _trim_optional_series(night_gap_max_points, decimals=2)
        night_gap_avg_display = _trim_optional_series(night_gap_avg_points, decimals=2)
        day_gap_max_period_display = _mask_series_with_numeric(day_gap_max_periods, day_gap_max_display)
        night_gap_max_period_display = _mask_series_with_numeric(
            night_gap_max_periods, night_gap_max_display
        )
        if day_gap_max_display is None:
            day_gap_max_display = [None] * len(labels)
        if day_gap_max_period_display is None:
            day_gap_max_period_display = [None] * len(labels)
        if day_gap_avg_display is None:
            day_gap_avg_display = [None] * len(labels)
        if night_gap_max_display is None:
            night_gap_max_display = [None] * len(labels)
        if night_gap_max_period_display is None:
            night_gap_max_period_display = [None] * len(labels)
        if night_gap_avg_display is None:
            night_gap_avg_display = [None] * len(labels)

        all_babies_split[str(night_start_hour)] = {
            "day": {
                "maxGap": day_gap_max_display,
                "maxGapPeriod": day_gap_max_period_display,
                "avgGap": day_gap_avg_display,
            },
            "night": {
                "maxGap": night_gap_max_display,
                "maxGapPeriod": night_gap_max_period_display,
                "avgGap": night_gap_avg_display,
            },
        }

    series = []
    series_child_keys = sorted(
        set(by_child_day.keys())
        | set(diaper_counts_by_child_day.keys())
        | set(gap_stats_by_child_day.keys()),
        key=lambda key: (child_map.get(key) or key),
    )
    for idx, child_key in enumerate(series_child_keys):
        day_totals = by_child_day.get(child_key, {})
        day_hour_totals = by_child_day_hour.get(child_key, {})
        day_feed_counts = feed_counts_by_child_day.get(child_key, {})
        day_hour_feed_counts = feed_counts_by_child_day_hour.get(child_key, {})
        diaper_day_counts = diaper_counts_by_child_day.get(child_key, {})
        diaper_day_hour_counts = diaper_counts_by_child_day_hour.get(child_key, {})
        child_gap_stats = gap_stats_by_child_day.get(child_key, {})
        child_gap_hour_stats = gap_stats_by_child_day_hour.get(child_key, {})

        running_total = 0.0
        daily_points = []
        cumulative_points = []
        avg_milk_per_feed_points = []
        diaper_points = []
        max_gap_points = []
        max_gap_periods = []
        avg_gap_points = []
        for day_key in labels:
            daily_value = day_totals.get(day_key, 0.0)
            daily_feed_count = int(day_feed_counts.get(day_key, 0))
            running_total += daily_value
            daily_points.append(daily_value)
            cumulative_points.append(running_total)
            avg_milk_per_feed_points.append(
                daily_value / daily_feed_count if daily_feed_count > 0 else None
            )
            diaper_points.append(diaper_day_counts.get(day_key, 0))

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

        max_gap_display = _trim_optional_series(max_gap_points, decimals=2)
        max_gap_period_display = _mask_series_with_numeric(max_gap_periods, max_gap_display)
        avg_gap_display = _trim_optional_series(avg_gap_points, decimals=2)
        avg_milk_per_feed_display = _trim_optional_series(avg_milk_per_feed_points, decimals=1)
        diaper_display = _trim_count_series(diaper_points)
        if max_gap_display is None:
            max_gap_display = [None] * len(labels)
        if max_gap_period_display is None:
            max_gap_period_display = [None] * len(labels)
        if avg_gap_display is None:
            avg_gap_display = [None] * len(labels)
        if avg_milk_per_feed_display is None:
            avg_milk_per_feed_display = [None] * len(labels)
        if diaper_display is None:
            diaper_display = [None] * len(labels)

        split = {}
        for night_start_hour in range(24):
            day_daily_points = []
            day_cumulative_points = []
            day_running_total = 0.0
            night_daily_points = []
            night_cumulative_points = []
            night_running_total = 0.0
            day_avg_milk_per_feed_points = []
            night_avg_milk_per_feed_points = []
            day_diaper_points = []
            night_diaper_points = []
            day_gap_max_points = []
            day_gap_max_periods = []
            day_gap_avg_points = []
            night_gap_max_points = []
            night_gap_max_periods = []
            night_gap_avg_points = []
            for day_key in labels:
                hour_totals = day_hour_totals.get(day_key, {})
                day_value = 0.0
                night_value = 0.0
                for hour, amount in hour_totals.items():
                    if is_night_hour(hour, night_start_hour):
                        night_value += amount
                    else:
                        day_value += amount

                hour_feed_counts = day_hour_feed_counts.get(day_key, {})
                day_feed_count = 0
                night_feed_count = 0
                for hour, count in hour_feed_counts.items():
                    if is_night_hour(hour, night_start_hour):
                        night_feed_count += count
                    else:
                        day_feed_count += count

                day_running_total += day_value
                day_daily_points.append(day_value)
                day_cumulative_points.append(day_running_total)
                day_avg_milk_per_feed_points.append(
                    day_value / day_feed_count if day_feed_count > 0 else None
                )
                night_running_total += night_value
                night_daily_points.append(night_value)
                night_cumulative_points.append(night_running_total)
                night_avg_milk_per_feed_points.append(
                    night_value / night_feed_count if night_feed_count > 0 else None
                )

                diaper_hour_counts = diaper_day_hour_counts.get(day_key, {})
                day_diaper_value = 0
                night_diaper_value = 0
                for hour, count in diaper_hour_counts.items():
                    if is_night_hour(hour, night_start_hour):
                        night_diaper_value += count
                    else:
                        day_diaper_value += count
                day_diaper_points.append(day_diaper_value)
                night_diaper_points.append(night_diaper_value)

                gap_hour_stats = child_gap_hour_stats.get(day_key, {})
                day_gap_sum = 0.0
                day_gap_count = 0
                day_gap_max = None
                day_gap_period = None
                night_gap_sum = 0.0
                night_gap_count = 0
                night_gap_max = None
                night_gap_period = None
                for hour, stat in gap_hour_stats.items():
                    gap_sum = float(stat.get("sum", 0.0))
                    gap_count = int(stat.get("count", 0))
                    gap_max = float(stat.get("max", 0.0))
                    if gap_count <= 0:
                        continue
                    if is_night_hour(hour, night_start_hour):
                        night_gap_sum += gap_sum
                        night_gap_count += gap_count
                        if night_gap_max is None or gap_max > night_gap_max:
                            night_gap_max = gap_max
                            night_gap_period = _format_gap_period(
                                stat.get("maxStart"), stat.get("maxEnd")
                            )
                    else:
                        day_gap_sum += gap_sum
                        day_gap_count += gap_count
                        if day_gap_max is None or gap_max > day_gap_max:
                            day_gap_max = gap_max
                            day_gap_period = _format_gap_period(
                                stat.get("maxStart"), stat.get("maxEnd")
                            )

                day_gap_max_points.append(day_gap_max)
                day_gap_max_periods.append(day_gap_period)
                day_gap_avg_points.append(day_gap_sum / day_gap_count if day_gap_count > 0 else None)
                night_gap_max_points.append(night_gap_max)
                night_gap_max_periods.append(night_gap_period)
                night_gap_avg_points.append(night_gap_sum / night_gap_count if night_gap_count > 0 else None)

            day_daily_display, day_cumulative_display = _trim_milk_series(
                day_daily_points, day_cumulative_points
            )
            night_daily_display, night_cumulative_display = _trim_milk_series(
                night_daily_points, night_cumulative_points
            )
            if day_daily_display is None or day_cumulative_display is None:
                day_daily_display = [None] * len(labels)
                day_cumulative_display = [None] * len(labels)
            if night_daily_display is None or night_cumulative_display is None:
                night_daily_display = [None] * len(labels)
                night_cumulative_display = [None] * len(labels)

            day_gap_max_display = _trim_optional_series(day_gap_max_points, decimals=2)
            day_gap_avg_display = _trim_optional_series(day_gap_avg_points, decimals=2)
            night_gap_max_display = _trim_optional_series(night_gap_max_points, decimals=2)
            night_gap_avg_display = _trim_optional_series(night_gap_avg_points, decimals=2)
            day_avg_milk_per_feed_display = _trim_optional_series(
                day_avg_milk_per_feed_points, decimals=1
            )
            night_avg_milk_per_feed_display = _trim_optional_series(
                night_avg_milk_per_feed_points, decimals=1
            )
            day_gap_max_period_display = _mask_series_with_numeric(day_gap_max_periods, day_gap_max_display)
            night_gap_max_period_display = _mask_series_with_numeric(
                night_gap_max_periods, night_gap_max_display
            )
            day_diaper_display = _trim_count_series(day_diaper_points)
            night_diaper_display = _trim_count_series(night_diaper_points)
            if day_gap_max_display is None:
                day_gap_max_display = [None] * len(labels)
            if day_gap_max_period_display is None:
                day_gap_max_period_display = [None] * len(labels)
            if day_gap_avg_display is None:
                day_gap_avg_display = [None] * len(labels)
            if day_avg_milk_per_feed_display is None:
                day_avg_milk_per_feed_display = [None] * len(labels)
            if night_gap_max_display is None:
                night_gap_max_display = [None] * len(labels)
            if night_gap_max_period_display is None:
                night_gap_max_period_display = [None] * len(labels)
            if night_gap_avg_display is None:
                night_gap_avg_display = [None] * len(labels)
            if night_avg_milk_per_feed_display is None:
                night_avg_milk_per_feed_display = [None] * len(labels)
            if day_diaper_display is None:
                day_diaper_display = [None] * len(labels)
            if night_diaper_display is None:
                night_diaper_display = [None] * len(labels)

            split[str(night_start_hour)] = {
                "day": {
                    "daily": day_daily_display,
                    "cumulative": day_cumulative_display,
                    "avgMilkPerFeed": day_avg_milk_per_feed_display,
                    "diaper": day_diaper_display,
                    "maxGap": day_gap_max_display,
                    "maxGapPeriod": day_gap_max_period_display,
                    "avgGap": day_gap_avg_display,
                },
                "night": {
                    "daily": night_daily_display,
                    "cumulative": night_cumulative_display,
                    "avgMilkPerFeed": night_avg_milk_per_feed_display,
                    "diaper": night_diaper_display,
                    "maxGap": night_gap_max_display,
                    "maxGapPeriod": night_gap_max_period_display,
                    "avgGap": night_gap_avg_display,
                },
            }

        series.append(
            {
                "label": child_map.get(child_key) or child_key,
                "daily": daily_display,
                "cumulative": cumulative_display,
                "avgMilkPerFeed": avg_milk_per_feed_display,
                "diaper": diaper_display,
                "maxGap": max_gap_display,
                "maxGapPeriod": max_gap_period_display,
                "avgGap": avg_gap_display,
                "split": split,
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
            "split": all_babies_split,
        },
        "defaultNightStart": 20,
        "skippedUnits": skipped_units,
    }


def build_plot_html(events, child_map, generated_at):
    chart_data = milk_totals_by_day(events, child_map)
    chart_data_json = json.dumps(chart_data, separators=(",", ":"))
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
    const defaultNightStart = Number(payload.defaultNightStart ?? 20);
    const hiddenSeriesKeys = new Set();

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

    const todayLabel = localDayKey(new Date());
    const todayIndex = labels.indexOf(todayLabel);

    function hasAnyValue(values) {{
      return Array.isArray(values) && values.some((value) => value != null);
    }}

    function isMilkMode(plotMode) {{
      return plotMode === "milk-daily" || plotMode === "milk-cumulative" || plotMode === "milk-average-feed";
    }}

    function isCumulativeMode(plotMode) {{
      return plotMode === "milk-cumulative";
    }}

    function isSmoothable(plotMode) {{
      return plotMode !== "milk-cumulative";
    }}

    function plotModeLabel(plotMode) {{
      if (plotMode === "milk-cumulative") {{
        return "cumulative milk";
      }}
      if (plotMode === "milk-average-feed") {{
        return "avg milk per feed";
      }}
      if (plotMode === "diaper-daily") {{
        return "daily diapers";
      }}
      if (plotMode === "gap-max") {{
        return "max gap";
      }}
      if (plotMode === "gap-avg") {{
        return "avg gap";
      }}
      return "daily milk";
    }}

    function plotUnit(plotMode) {{
      if (isMilkMode(plotMode)) {{
        return "mL";
      }}
      if (plotMode === "diaper-daily") {{
        return "changes";
      }}
      return "h";
    }}

    function plotValueDecimals(plotMode, smoothWindow) {{
      if (isMilkMode(plotMode)) {{
        return 1;
      }}
      if (plotMode === "diaper-daily") {{
        return smoothWindow > 1 ? 1 : 0;
      }}
      return 2;
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

    function splitSeriesValues(entry, plotMode, nightStartHour, period) {{
      const splitByHour = entry.split || {{}};
      const split = splitByHour[String(nightStartHour)] || null;
      if (!split || !split[period]) {{
        return [];
      }}
      if (plotMode === "milk-cumulative") {{
        return split[period].cumulative || [];
      }}
      if (plotMode === "milk-average-feed") {{
        return split[period].avgMilkPerFeed || [];
      }}
      if (plotMode === "gap-max") {{
        return split[period].maxGap || [];
      }}
      if (plotMode === "gap-avg") {{
        return split[period].avgGap || [];
      }}
      if (plotMode === "diaper-daily") {{
        return split[period].diaper || [];
      }}
      return split[period].daily || [];
    }}

    function splitSeriesMaxGapPeriods(entry, nightStartHour, period) {{
      const splitByHour = entry.split || {{}};
      const split = splitByHour[String(nightStartHour)] || null;
      if (!split || !split[period]) {{
        return [];
      }}
      return split[period].maxGapPeriod || [];
    }}

    function modeSeriesValues(entry, plotMode) {{
      if (plotMode === "milk-cumulative") {{
        return entry.cumulative || [];
      }}
      if (plotMode === "milk-average-feed") {{
        return entry.avgMilkPerFeed || [];
      }}
      if (plotMode === "gap-max") {{
        return entry.maxGap || [];
      }}
      if (plotMode === "gap-avg") {{
        return entry.avgGap || [];
      }}
      if (plotMode === "diaper-daily") {{
        return entry.diaper || [];
      }}
      return entry.daily || [];
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
      const splitByHour = allBabiesGap.split || {{}};
      const split = splitByHour[String(nightStartHour)] || null;
      if (!split || !split[period]) {{
        return [];
      }}
      if (plotMode === "gap-max") {{
        return split[period].maxGap || [];
      }}
      if (plotMode === "gap-avg") {{
        return split[period].avgGap || [];
      }}
      return [];
    }}

    function allBabiesSplitGapPeriods(nightStartHour, period) {{
      if (!allBabiesGap) {{
        return [];
      }}
      const splitByHour = allBabiesGap.split || {{}};
      const split = splitByHour[String(nightStartHour)] || null;
      if (!split || !split[period]) {{
        return [];
      }}
      return split[period].maxGapPeriod || [];
    }}

    function buildDatasets(plotMode, smoothWindow, splitEnabled, nightStartHour) {{
      const datasets = [];
      series.forEach((entry) => {{
        if (!splitEnabled) {{
          const raw = modeSeriesValues(entry, plotMode);
          const maxGapPeriods = plotMode === "gap-max" ? modeSeriesMaxGapPeriods(entry) : null;
          const data = isSmoothable(plotMode) ? movingAverage(raw, smoothWindow, todayIndex) : raw;
          if (!hasAnyValue(data)) {{
            return;
          }}
          const customKey = `single:${{plotMode}}:${{entry.label}}`;
          datasets.push({{
            label: entry.label,
            customKey,
            data,
            borderColor: entry.borderColor,
            backgroundColor: entry.backgroundColor,
            pointRadius: 1,
            pointHoverRadius: 4,
            borderWidth: 2,
            tension: 0.2,
            spanGaps: false,
            hidden: hiddenSeriesKeys.has(customKey),
            maxGapPeriods,
          }});
          return;
        }}

        const periodSpecs = [
          {{ period: "day", label: "Day", dash: [] }},
          {{ period: "night", label: "Night", dash: [8, 5] }},
        ];
        periodSpecs.forEach((spec) => {{
          const raw = splitSeriesValues(entry, plotMode, nightStartHour, spec.period);
          const maxGapPeriods =
            plotMode === "gap-max" ? splitSeriesMaxGapPeriods(entry, nightStartHour, spec.period) : null;
          const data = isSmoothable(plotMode) ? movingAverage(raw, smoothWindow, todayIndex) : raw;
          if (!hasAnyValue(data)) {{
            return;
          }}
          const customKey = `split:${{plotMode}}:${{entry.label}}:${{spec.period}}`;
          datasets.push({{
            label: `${{entry.label}} (${{spec.label}})`,
            customKey,
            data,
            borderColor: entry.borderColor,
            backgroundColor: entry.backgroundColor,
            borderDash: spec.dash,
            pointRadius: 1,
            pointHoverRadius: 4,
            borderWidth: 2,
            tension: 0.2,
            spanGaps: false,
            hidden: hiddenSeriesKeys.has(customKey),
            maxGapPeriods,
          }});
        }});
      }});

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
          datasets.push({{
            label: allBabiesGap.label || "All Babies",
            customKey,
            data,
            borderColor: "#ffffff",
            backgroundColor: "#ffffff",
            pointRadius: 1,
            pointHoverRadius: 4,
            borderWidth: 3,
            tension: 0.2,
            spanGaps: false,
            hidden: hiddenSeriesKeys.has(customKey),
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
        const labelBase = allBabiesGap.label || "All Babies";
        datasets.push({{
          label: `${{labelBase}} (${{spec.label}})`,
          customKey,
          data,
          borderColor: "#ffffff",
          backgroundColor: "#ffffff",
          borderDash: spec.dash,
          pointRadius: 1,
          pointHoverRadius: 4,
          borderWidth: 3,
          tension: 0.2,
          spanGaps: false,
          hidden: hiddenSeriesKeys.has(customKey),
          maxGapPeriods,
        }});
      }});
      return datasets;
    }}

    function yAxisTitle(plotMode, smoothWindow, splitEnabled, nightStartHour) {{
      let baseTitle = "";
      if (plotMode === "milk-cumulative") {{
        baseTitle = "Cumulative milk eaten (mL)";
      }} else if (plotMode === "milk-average-feed") {{
        baseTitle = smoothWindow <= 1
          ? "Average milk per feed (mL)"
          : `Average milk per feed (mL, ${{smoothWindow}}-day moving avg)`;
      }} else if (plotMode === "diaper-daily") {{
        baseTitle = "Diaper changes per day";
      }} else if (plotMode === "gap-max") {{
        baseTitle = "Max feeding gap per day (hours)";
      }} else if (plotMode === "gap-avg") {{
        baseTitle = "Average feeding gap per day (hours)";
      }} else if (smoothWindow <= 1) {{
        baseTitle = "Milk eaten per day (mL)";
      }} else {{
        baseTitle = `Milk eaten per day (mL, ${{smoothWindow}}-day moving avg)`;
      }}
      if (plotMode !== "milk-cumulative" && !isMilkMode(plotMode) && smoothWindow > 1) {{
        baseTitle = `${{baseTitle}} (${{smoothWindow}}-day moving avg)`;
      }}
      if (!splitEnabled) {{
        return baseTitle;
      }}
      return `${{baseTitle}} (Day/Night split, night ${{splitWindowText(nightStartHour)}})`;
    }}

    function smoothingText(plotMode, smoothWindow) {{
      if (!isSmoothable(plotMode)) {{
        return "Smoothing disabled in cumulative mode";
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
      const dayKey = labels[idx] || "";
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

    function updateVisibleRange(chart) {{
      const textEl = document.getElementById("visible-range");
      if (!textEl || !labels.length) {{
        return;
      }}
      const xScale = chart.scales.x;
      const minIdx = Math.max(0, Math.ceil(xScale.min ?? 0));
      const maxIdx = Math.min(labels.length - 1, Math.floor(xScale.max ?? labels.length - 1));
      textEl.textContent = `Visible range: ${{labels[minIdx]}} to ${{labels[maxIdx]}}`;
    }}

    function updateControlStates(plotMode, splitEnabled, modeSelect, smoothSlider, splitToggle, nightStartSelect) {{
      if (modeSelect) {{
        modeSelect.disabled = false;
      }}
      if (smoothSlider) {{
        smoothSlider.disabled = !isSmoothable(plotMode);
      }}
      const splitAvailable = true;
      if (splitToggle) {{
        splitToggle.disabled = !splitAvailable;
      }}
      if (nightStartSelect) {{
        nightStartSelect.disabled = !(splitAvailable && splitEnabled);
      }}
    }}

    function applyHiddenSeriesState(targetChart) {{
      if (!targetChart) {{
        return;
      }}
      targetChart.data.datasets.forEach((dataset, idx) => {{
        const key = dataset.customKey || dataset.label || String(idx);
        targetChart.setDatasetVisibility(idx, !hiddenSeriesKeys.has(key));
      }});
    }}

    const canvas = document.getElementById("milk-chart");
    const noData = document.getElementById("no-data");
    const chartWrap = document.querySelector(".chart-wrap");
    const modeSelect = document.getElementById("series-mode");
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

    let chart = null;
    if (!labels.length || !series.length) {{
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
      updateSmoothingLabel("milk-daily", 1);
      updateSplitLabel(false, defaultNightStart);
    }} else {{
      const initialMode = "milk-daily";
      const initialSmoothWindow = 1;
      const initialSplitEnabled = false;
      const initialNightStart = defaultNightStart;
      chart = new Chart(canvas, {{
        type: "line",
        data: {{
          labels,
          datasets: buildDatasets(initialMode, initialSmoothWindow, initialSplitEnabled, initialNightStart),
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          interaction: {{
            mode: "nearest",
            intersect: false,
          }},
          plugins: {{
            legend: {{
              labels: {{ color: "#f2f2f2" }},
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
                }} else {{
                  hiddenSeriesKeys.delete(key);
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
                  const windowSize = context.chart.$smoothWindow || 1;
                  const smoothSuffix = isSmoothable(plotMode) && windowSize > 1 ? `, ${{windowSize}}d MA` : "";
                  const splitSuffix = context.chart.$splitEnabled ? ", split" : "";
                  const unit = plotUnit(plotMode);
                  const decimals = plotValueDecimals(plotMode, windowSize);
                  const mainLine = `${{context.dataset.label}}: ${{context.parsed.y.toFixed(decimals)}} ${{unit}} (${{plotModeLabel(plotMode)}}${{smoothSuffix}}${{splitSuffix}})`;
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
                onZoomComplete: ({{ chart }}) => updateVisibleRange(chart),
              }},
              onPanComplete: ({{ chart }}) => updateVisibleRange(chart),
            }},
          }},
          scales: {{
            x: {{
              ticks: {{ color: "#d2d2d2", maxTicksLimit: 12 }},
              grid: {{ color: "rgba(255,255,255,0.08)" }},
              title: {{ display: true, color: "#d2d2d2", text: "Day" }},
            }},
            y: {{
              ticks: {{ color: "#d2d2d2" }},
              grid: {{ color: "rgba(255,255,255,0.08)" }},
              title: {{ display: true, color: "#d2d2d2", text: yAxisTitle(initialMode, initialSmoothWindow, initialSplitEnabled, initialNightStart) }},
            }},
          }},
        }},
      }});
      chart.$mode = initialMode;
      chart.$smoothWindow = initialSmoothWindow;
      chart.$splitEnabled = initialSplitEnabled;
      chart.$nightStart = initialNightStart;
      if (modeSelect) {{
        modeSelect.value = initialMode;
      }}
      if (smoothSlider) {{
        smoothSlider.value = String(initialSmoothWindow);
      }}
      if (splitToggle) {{
        splitToggle.checked = initialSplitEnabled;
      }}
      if (nightStartSelect) {{
        nightStartSelect.value = String(initialNightStart);
      }}
      updateControlStates(initialMode, initialSplitEnabled, modeSelect, smoothSlider, splitToggle, nightStartSelect);
      updateSmoothingLabel(initialMode, initialSmoothWindow);
      updateSplitLabel(initialSplitEnabled, initialNightStart);
      updateVisibleRange(chart);
    }}

    function refreshChart(animationMode) {{
      if (!chart) {{
        return;
      }}
      const mode = chart.$mode || "milk-daily";
      const smoothWindow = chart.$smoothWindow || 1;
      const splitEnabled = currentSplitEnabled();
      const nightStart = currentNightStart();
      chart.$splitEnabled = splitEnabled;
      chart.$nightStart = nightStart;
      updateControlStates(mode, splitEnabled, modeSelect, smoothSlider, splitToggle, nightStartSelect);
      chart.data.datasets = buildDatasets(mode, smoothWindow, splitEnabled, nightStart);
      applyHiddenSeriesState(chart);
      chart.options.scales.y.title.text = yAxisTitle(mode, smoothWindow, splitEnabled, nightStart);
      chart.update(animationMode);
      updateSmoothingLabel(mode, smoothWindow);
      updateSplitLabel(splitEnabled, nightStart);
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
      updateVisibleRange(chart);
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
    <p class=\"subtitle\">Choose a plot (milk totals, diaper counts, or feeding gaps). Day/Night split uses a 12-hour night window from the selected start time (default 20:00). Drag horizontally to zoom; hold Shift and drag to pan.</p>
    <div class=\"actions\">
      <a class=\"btn\" href=\"/\">Back to Main View</a>
      <select id=\"series-mode\" class=\"mode-select\" aria-label=\"Series mode\">
        <option value=\"milk-daily\" selected>Daily Milk Total</option>
        <option value=\"milk-cumulative\">Cumulative Milk Total</option>
        <option value=\"milk-average-feed\">Average Milk Per Feed</option>
        <option value=\"diaper-daily\">Daily Diaper Changes</option>
        <option value=\"gap-max\">Max Feeding Gap</option>
        <option value=\"gap-avg\">Average Feeding Gap</option>
      </select>
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
        if parsed.path in ("/milk", "/milk.html"):
            self.send_response(302)
            self.send_header("Location", "/plot")
            self.end_headers()
            return
        if parsed.path not in ("/", "/index.html", "/json", "/plot", "/plot.html"):
            self.send_response(404)
            self.end_headers()
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
