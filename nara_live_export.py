import argparse
import json
import logging
import os
import sqlite3
import subprocess
import time
from pathlib import Path


REMOTE_NARA_DB = "/data/data/com.naraorganics.nara/no_backup/NaraSqlite/nara.db"
REMOTE_FIREBASE_DB = "/data/data/com.naraorganics.nara/databases/amazing-ripple-221320.firebaseio.com_default"
DEFAULT_ADB_TIMEOUT_SECONDS = 10.0


class CommandTimeoutError(RuntimeError):
    pass


def command_text(cmd):
    return " ".join(str(part) for part in cmd)


def output_text(value):
    if not value:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip()


def run(cmd, timeout=None, **subprocess_options):
    subprocess_options.setdefault("stdin", subprocess.DEVNULL)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            **subprocess_options,
        )
    except subprocess.TimeoutExpired as exc:
        details = []
        stdout = output_text(exc.stdout)
        stderr = output_text(exc.stderr)
        if stdout:
            details.append(f"stdout: {stdout}")
        if stderr:
            details.append(f"stderr: {stderr}")
        output = "\n" + "\n".join(details) if details else ""
        timeout_text = f"{timeout:g}" if timeout is not None else "unknown"
        raise CommandTimeoutError(
            f"Command timed out after {timeout_text}s: {command_text(cmd)}{output}"
        ) from exc
    if result.returncode != 0:
        output = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: {command_text(cmd)}\n{output}"
        )
    return result.stdout


def adb_timeout_seconds():
    value = os.environ.get("NARA_ADB_TIMEOUT") or str(DEFAULT_ADB_TIMEOUT_SECONDS)
    try:
        timeout = float(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid NARA_ADB_TIMEOUT value: {value!r}") from exc
    if timeout <= 0:
        raise RuntimeError(f"NARA_ADB_TIMEOUT must be positive: {value!r}")
    return timeout


def adb_command(adb_path, adb_device, *args):
    cmd = [adb_path]
    if adb_device:
        cmd.extend(["-s", adb_device])
    cmd.extend(args)
    return cmd


def permission_denied_error(exc):
    return "Permission denied" in str(exc)


def adb_root(adb_path, adb_device=None, timeout=None):
    if timeout is None:
        timeout = adb_timeout_seconds()
    logging.info("Running adb root for %s", adb_device or "default device")
    run(adb_command(adb_path, adb_device, "root"), timeout=timeout)
    run(adb_command(adb_path, adb_device, "wait-for-device"), timeout=timeout)
    time.sleep(0.5)
    logging.info("ADB root completed for %s", adb_device or "default device")


def adb_pull(adb_path, remote, local, adb_device=None, retries=2, retry_delay=0.5, timeout=None):
    cmd = adb_command(adb_path, adb_device, "pull", remote, str(local))
    if timeout is None:
        timeout = adb_timeout_seconds()

    last_exc = None
    root_attempted = False
    attempt = 0
    while attempt <= retries:
        try:
            return run(cmd, timeout=timeout)
        except CommandTimeoutError:
            raise
        except RuntimeError as exc:
            last_exc = exc
            if not root_attempted and permission_denied_error(exc):
                root_attempted = True
                logging.warning(
                    "ADB pull got permission denied for %s; running adb root and retrying",
                    remote,
                )
                try:
                    adb_root(adb_path, adb_device, timeout=timeout)
                except CommandTimeoutError:
                    raise
                except RuntimeError as root_exc:
                    raise RuntimeError(
                        "ADB pull failed with permission denied, and adb root failed.\n"
                        f"Pull error: {exc}\n"
                        f"Root error: {root_exc}"
                    ) from root_exc
                continue
            if attempt >= retries:
                break
            time.sleep(retry_delay * (attempt + 1))
            attempt += 1
    if last_exc is not None:
        raise last_exc


def load_json_blob(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="ignore")
    else:
        text = str(value)
    text = text.strip()
    if not text or text == "null":
        return None
    return json.loads(text)


def load_child_map(firebase_db_path, family_keys):
    child_map = {}
    if not firebase_db_path.exists():
        return child_map
    con = sqlite3.connect(firebase_db_path)
    cur = con.cursor()
    for family_key in family_keys:
        path = f"/familyz/{family_key}/childz/"
        cur.execute("SELECT value FROM serverCache WHERE path = ?", (path,))
        row = cur.fetchone()
        if not row:
            continue
        data = load_json_blob(row[0]) or {}
        for child_key, child in data.items():
            name = child.get("name") if isinstance(child, dict) else None
            if name:
                child_map[child_key] = name
    con.close()
    return child_map


def load_user_map(firebase_db_path):
    user_map = {}
    if not firebase_db_path.exists():
        return user_map
    con = sqlite3.connect(firebase_db_path)
    cur = con.cursor()
    cur.execute("SELECT path, value FROM serverCache WHERE path LIKE '/userz/%/_/'")
    for path, value in cur.fetchall():
        parts = path.split("/")
        if len(parts) < 4:
            continue
        user_key = parts[2]
        data = load_json_blob(value) or {}
        name = data.get("name") if isinstance(data, dict) else None
        if name:
            user_map[user_key] = name
    con.close()
    return user_map


def collect_live_data(nara_db_path, firebase_db_path, limit=None):
    con = sqlite3.connect(nara_db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT DISTINCT familyKey FROM trackz")
    family_keys = [r[0] for r in cur.fetchall() if r[0]]

    child_map = load_child_map(firebase_db_path, family_keys)
    user_map = load_user_map(firebase_db_path)

    sql = "SELECT key, etag, updateDt, json, beginDt, endDt, familyKey, childKey, trackGroupKey, trackTypeKey, formulaName, medicineName, note FROM trackz ORDER BY beginDt DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur.execute(sql)

    events = []
    for row in cur.fetchall():
        payload = load_json_blob(row["json"]) or {}
        create_user_key = payload.get("createUserKey") or payload.get("userKey")
        event = {
            "key": row["key"],
            "familyKey": row["familyKey"],
            "childKey": row["childKey"],
            "childName": child_map.get(row["childKey"]),
            "trackGroupKey": row["trackGroupKey"],
            "trackTypeKey": row["trackTypeKey"],
            "beginDt": row["beginDt"],
            "endDt": row["endDt"],
            "note": row["note"],
            "createUserKey": create_user_key,
            "createUserName": user_map.get(create_user_key),
            "payload": payload,
        }
        events.append(event)

    con.close()

    return {
        "generatedAt": int(time.time() * 1000),
        "familyKeys": family_keys,
        "children": child_map,
        "users": user_map,
        "events": events,
    }


def export_live(nara_db_path, firebase_db_path, out_path, limit=None):
    out = collect_live_data(nara_db_path, firebase_db_path, limit)
    out_path.write_text(json.dumps(out, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb-path", dest="adb_path", default=os.environ.get("ADB_PATH", "adb"))
    parser.add_argument("--out", dest="out_path", default="nara_live.json")
    parser.add_argument(
        "--adb-device",
        dest="adb_device",
        default=os.environ.get("ADB_DEVICE") or os.environ.get("ANDROID_SERIAL"),
    )
    parser.add_argument("--limit", dest="limit", type=int, default=None)
    parser.add_argument("--watch", dest="watch", action="store_true")
    parser.add_argument("--interval", dest="interval", type=int, default=60)
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent.relative_to(os.getcwd())
    db_dir = base_dir / "nara_device_db"
    db_dir.mkdir(exist_ok=True)

    nara_db_path = db_dir / "nara.db"
    firebase_db_path = db_dir / "amazing-ripple-221320.firebaseio.com_default"
    out_path = base_dir / args.out_path

    while True:
        adb_pull(args.adb_path, REMOTE_NARA_DB, nara_db_path, args.adb_device)
        adb_pull(args.adb_path, REMOTE_FIREBASE_DB, firebase_db_path, args.adb_device)
        export_live(nara_db_path, firebase_db_path, out_path, args.limit)
        if not args.watch:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
