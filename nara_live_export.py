import argparse
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path


REMOTE_NARA_DB = "/data/data/com.naraorganics.nara/no_backup/NaraSqlite/nara.db"
REMOTE_FIREBASE_DB = "/data/data/com.naraorganics.nara/databases/amazing-ripple-221320.firebaseio.com_default"


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "command failed")
    return result.stdout


def adb_pull(adb_path, remote, local, adb_device=None, retries=2, retry_delay=0.5):
    cmd = [adb_path]
    if adb_device:
        cmd.extend(["-s", adb_device])
    cmd.extend(["pull", remote, str(local)])

    last_exc = None
    for attempt in range(retries + 1):
        try:
            return run(cmd)
        except RuntimeError as exc:
            last_exc = exc
            if attempt >= retries:
                break
            time.sleep(retry_delay * (attempt + 1))
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
