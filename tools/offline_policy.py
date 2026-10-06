#!/usr/bin/env python3
"""Local durable learner for symbolic app/site decisions.

Verified feedback updates exact-context action counts. It does not control the
guest; callers must ground actions and verify outcomes independently.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
import re
import sqlite3
import statistics
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from small_decider import symbol, unique_pairs

SCHEMA = "virtualos.offline-policy/v1"
MAX_REQUEST = 4096
MAX_DB = 4_194_304
APP = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(?:[.][A-Za-z0-9_-]+)+$")
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REVIEW = re.compile(r"^[a-z0-9][a-z0-9._-]{7,95}$")
DOCK_SCOPES = {"Finder": "com.apple.finder", "Terminal": "com.apple.Terminal",
               "Safari": "com.apple.Safari", "Google Chrome": "com.google.Chrome"}
SOURCE_APPS = frozenset(DOCK_SCOPES)
SIDEBAR_VIEWS = {"Applications", "Downloads", "Documents", "Desktop", "Recents"}
TRANSFERABLE_ACTIONS = {("app", "com.apple.finder", "sidebar.v1", "finder_sidebar_click")}


def fields(value: object, names: set[str]) -> dict:
    if type(value) is not dict or set(value) != names:
        raise ValueError("missing or unexpected fields")
    return value


def validate_context(value: object) -> dict:
    row = fields(value, {"scope", "goal", "facts"})
    scope = fields(row["scope"], {"kind", "id", "version"})
    kind, identity, version = scope["kind"], scope["id"], scope["version"]
    if type(kind) is not str or kind not in {"app", "site"}:
        raise ValueError("invalid scope kind")
    if type(identity) is not str or len(identity) > 253:
        raise ValueError("invalid scope ID")
    if kind == "app":
        if not APP.fullmatch(identity):
            raise ValueError("invalid app bundle ID")
    else:
        parsed = urlsplit(identity)
        if (parsed.scheme != "https" or not parsed.hostname
                or not re.fullmatch(r"(?:[a-z0-9-]+[.])+[a-z]{2,63}", parsed.hostname)
                or parsed.netloc != parsed.hostname or parsed.geturl() != identity
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("site ID must be an HTTPS origin")
    if type(version) is not str or not VERSION.fullmatch(version):
        raise ValueError("invalid scope version")
    symbol(row["goal"])
    facts = row["facts"]
    if type(facts) is not list or not 1 <= len(facts) <= 8:
        raise ValueError("1-8 symbolic facts required")
    for fact in facts:
        symbol(fact)
    if len(set(facts)) != len(facts):
        raise ValueError("duplicate facts")
    return row


def validate_options(value: object) -> list[str]:
    if type(value) is not list or not 2 <= len(value) <= 9:
        raise ValueError("2-9 action options required")
    for item in value:
        symbol(item)
    if len(set(value)) != len(value) or "abstain" not in value:
        raise ValueError("distinct choices including abstain required")
    return value


def context_key(context: dict) -> str:
    canonical = {"scope": context["scope"], "goal": context["goal"],
                 "facts": sorted(context["facts"])}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or (path.exists() and path.stat().st_size > MAX_DB):
        raise ValueError("policy DB is symlinked or exceeds 4 MiB")
    connection = sqlite3.connect(path, timeout=3)
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA busy_timeout=3000")
    connection.execute("CREATE TABLE IF NOT EXISTS feedback (review_id TEXT PRIMARY KEY, context_key TEXT NOT NULL, action TEXT NOT NULL, outcome INTEGER NOT NULL)")
    connection.execute("CREATE INDEX IF NOT EXISTS feedback_context_action ON feedback(context_key, action)")
    connection.execute("CREATE TABLE IF NOT EXISTS contexts (context_key TEXT PRIMARY KEY, context_json TEXT NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS locators (review_id TEXT PRIMARY KEY, context_key TEXT NOT NULL, action TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL)")
    connection.execute("CREATE INDEX IF NOT EXISTS locators_context_action ON locators(context_key, action)")
    connection.execute("CREATE TABLE IF NOT EXISTS source_apps (review_id TEXT PRIMARY KEY, source_app TEXT NOT NULL)")
    # Earlier local feedback stored only context hashes. These four known Dock
    # contexts can be recovered without reading private trace text or pixels.
    for bundle in DOCK_SCOPES.values():
        context = {"scope": {"kind": "app", "id": bundle, "version": "dock.v1"},
                   "goal": "app.frontmost", "facts": ["app.not_frontmost"]}
        key = context_key(context)
        if connection.execute("SELECT 1 FROM feedback WHERE context_key=? LIMIT 1", (key,)).fetchone():
            connection.execute("INSERT OR IGNORE INTO contexts VALUES (?,?)",
                               (key, json.dumps(context, sort_keys=True, separators=(",", ":"))))
    connection.commit()
    path.chmod(0o600)
    return connection


def observe(connection: sqlite3.Connection, request: dict) -> dict:
    required = {"schema", "context", "action", "outcome", "verifier", "review_id", "rights"}
    if (type(request) is not dict or not required <= set(request)
            or set(request) - required - {"locator", "source_app"}):
        raise ValueError("missing or unexpected feedback fields")
    row = request
    if row["schema"] != SCHEMA:
        raise ValueError("unsupported feedback schema")
    context = validate_context(row["context"])
    action = symbol(row["action"])
    if (action == "abstain" or type(row["outcome"]) is not str
            or row["outcome"] not in {"verified_success", "verified_failure"}):
        raise ValueError("executed action and verified outcome required")
    verifier = fields(row["verifier"], {"kind", "state", "independent"})
    if (type(verifier["kind"]) is not str
            or verifier["kind"] not in {"ax", "ocr", "app_model", "filesystem"}
            or verifier["independent"] is not True):
        raise ValueError("independent symbolic verifier required")
    symbol(verifier["state"])
    review_id = row["review_id"]
    if type(review_id) is not str or not REVIEW.fullmatch(review_id):
        raise ValueError("review ID required")
    source_app = row.get("source_app")
    if source_app is not None and (type(source_app) is not str or source_app not in SOURCE_APPS):
        raise ValueError("unreviewed source app")
    rights = fields(row["rights"], {"ownership", "training", "consent_id"})
    training_allowed = (type(rights["ownership"]) is str
                        and rights["ownership"] in {"owned", "licensed"}
                        and rights["training"] == "allowed")
    local_only = (rights["ownership"] == "local-use"
                  and rights["training"] == "local-only")
    if not (training_allowed or local_only):
        raise ValueError("local-use or training rights required")
    symbol(rights["consent_id"])
    locator = row.get("locator")
    if locator is not None:
        fields(locator, {"x", "y"})
        if (row["outcome"] != "verified_success"
                or type(locator["x"]) not in (int, float)
                or type(locator["y"]) not in (int, float)
                or not 0 <= locator["x"] < 1
                or not 0.8 <= locator["y"] < 1):
            raise ValueError("verified Dock-region locator required")
    if connection.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] >= 4096:
        raise ValueError("feedback row limit reached")
    try:
        with connection:
            connection.execute("INSERT OR IGNORE INTO contexts VALUES (?,?)",
                               (context_key(context), json.dumps(context, sort_keys=True,
                                                                  separators=(",", ":"))))
            connection.execute("INSERT INTO feedback VALUES (?,?,?,?)",
                               (review_id, context_key(context), action,
                                int(row["outcome"] == "verified_success")))
            if locator is not None:
                connection.execute("INSERT INTO locators VALUES (?,?,?,?,?)",
                                   (review_id, context_key(context), action,
                                    locator["x"], locator["y"]))
            if source_app is not None:
                connection.execute("INSERT INTO source_apps VALUES (?,?)", (review_id, source_app))
    except sqlite3.IntegrityError as error:
        raise ValueError("review ID already recorded") from error
    return {"offline_learning": "recorded", "review_id": review_id,
            "stored_bytes": connection.execute("PRAGMA page_count").fetchone()[0]
            * connection.execute("PRAGMA page_size").fetchone()[0]}


def stable_locator(connection: sqlite3.Connection, key: str, action: str) -> dict | None:
    points = list(connection.execute("SELECT x,y FROM locators WHERE context_key=? AND action=?",
                                     (key, action)))
    if len(points) < 3:
        return None
    center_x = statistics.median(x for x, _ in points)
    center_y = statistics.median(y for _, y in points)
    inliers = [(x, y) for x, y in points
               if abs(x - center_x) <= 0.06 and abs(y - center_y) <= 0.04]
    if len(inliers) < 3:
        return None
    return {"x": round(statistics.median(x for x, _ in inliers), 6),
            "y": round(statistics.median(y for _, y in inliers), 6),
            "samples": len(inliers)}


def suggest(connection: sqlite3.Connection, request: dict) -> dict:
    if (type(request) is not dict or set(request) not in
            ({"schema", "context", "options"},
             {"schema", "context", "options", "source_app"})):
        raise ValueError("missing or unexpected request fields")
    row = request
    if row["schema"] != SCHEMA:
        raise ValueError("unsupported request schema")
    context = validate_context(row["context"])
    options = validate_options(row["options"])
    source_app = row.get("source_app")
    if source_app is not None and (type(source_app) is not str or source_app not in SOURCE_APPS):
        raise ValueError("unreviewed source app")
    counts = {}
    for action, successes, total in connection.execute(
            "SELECT action,SUM(outcome),COUNT(*) FROM feedback WHERE context_key=? GROUP BY action",
            (context_key(context),)):
        if action in options:
            counts[action] = {"success": successes, "failure": total - successes}
    # Do not act on a single example. Repeated independent verified successes
    # must outweigh failures; execution remains a separate reviewed step.
    qualified = [(action, data["success"] / (data["success"] + data["failure"]))
                 for action, data in counts.items()
                 if data["success"] >= 3 and
                 data["success"] / (data["success"] + data["failure"]) >= 0.8]
    selected = max(qualified, key=lambda item: item[1])[0] if len(qualified) == 1 else "abstain"
    source = "local_verified_feedback"
    source_counts = None
    if (source_app is not None and context["scope"]["version"] == "dock.v1"
            and "dock_app_click" in options):
        successes, total = connection.execute(
            "SELECT COALESCE(SUM(f.outcome),0),COUNT(*) FROM feedback f "
            "JOIN source_apps s ON s.review_id=f.review_id "
            "WHERE f.context_key=? AND f.action=? AND s.source_app=?",
            (context_key(context), "dock_app_click", source_app)).fetchone()
        source_counts = {"success": successes, "failure": total - successes}
        if total >= 3:
            selected = ("dock_app_click" if successes >= 3 and successes * 5 >= 4 * total
                        else "abstain")
            source = "source_app_verified_feedback"
    # This transfer profile shares only the action type. Each execution must
    # still locate the requested sidebar row in the current guest frame.
    scope = context["scope"]
    finder_goal = any(context["goal"] == f"finder.sidebar.{view.lower()}.visible"
                      for view in SIDEBAR_VIEWS)
    if (selected == "abstain" and finder_goal
            and counts.get("finder_sidebar_click", {}).get("failure", 0) == 0
            and set(context["facts"]) == {"finder.active", "view.not_visible"}):
        profile = (scope["kind"], scope["id"], scope["version"], "finder_sidebar_click")
        if profile in TRANSFERABLE_ACTIONS and "finder_sidebar_click" in options:
            successes = failures = 0
            goals = set()
            for goal_json, outcome in connection.execute(
                    "SELECT c.context_json,f.outcome FROM feedback f "
                    "JOIN contexts c ON c.context_key=f.context_key WHERE f.action=?",
                    ("finder_sidebar_click",)):
                recorded = json.loads(goal_json)
                recorded_goal = any(recorded["goal"] == f"finder.sidebar.{view.lower()}.visible"
                                    for view in SIDEBAR_VIEWS)
                if (recorded_goal and recorded["scope"] == scope
                        and set(recorded["facts"]) == set(context["facts"])):
                    goals.add(recorded["goal"])
                    successes += int(outcome == 1)
                    failures += int(outcome == 0)
            if len(goals) >= 3 and successes >= 3 and successes * 5 >= 4 * (successes + failures):
                selected = "finder_sidebar_click"
                source = "finder_sidebar_transfer"
    locator = stable_locator(connection, context_key(context), selected) if selected != "abstain" else None
    return {"choice": selected, "counts": counts, "locator": locator,
            "execution": "disabled", "scope": context["scope"],
            "source": source, "source_counts": source_counts}


def next_finder_target(connection: sqlite3.Connection,
                       available: set[str] | None = None) -> dict:
    choices = []
    for view in sorted(SIDEBAR_VIEWS):
        if available is not None and view not in available:
            continue
        context = {"scope": {"kind": "app", "id": "com.apple.finder", "version": "sidebar.v1"},
                   "goal": f"finder.sidebar.{view.lower()}.visible",
                   "facts": ["finder.active", "view.not_visible"]}
        success, failure = connection.execute(
            "SELECT COALESCE(SUM(outcome),0),COUNT(*)-COALESCE(SUM(outcome),0) "
            "FROM feedback WHERE context_key=? AND action=?",
            (context_key(context), "finder_sidebar_click")).fetchone()
        if success < 3 and failure < 3:
            choices.append((success, failure, view))
    if not choices:
        return {"target": None, "reason": "coverage_or_failure_limit"}
    success, failure, view = min(choices)
    return {"target": view, "success": success, "failure": failure,
            "reason": "least_verified_coverage"}


def read_request(path: Path) -> dict:
    if path.stat().st_size > MAX_REQUEST:
        raise ValueError("request exceeds 4 KiB")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if data[:2] != b"\xff\xd8":
        return None
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xff:
            return None
        marker = data[offset + 1]
        offset += 2
        if marker in (0xd8, 0x01) or 0xd0 <= marker <= 0xd7:
            continue
        if marker in (0xd9, 0xda):
            break
        size = int.from_bytes(data[offset:offset + 2], "big")
        if size < 2 or offset + size > len(data):
            return None
        if marker in (*range(0xc0, 0xc4), *range(0xc5, 0xc8),
                      *range(0xc9, 0xcc), *range(0xcd, 0xd0)):
            if size < 7:
                return None
            height = int.from_bytes(data[offset + 3:offset + 5], "big")
            width = int.from_bytes(data[offset + 5:offset + 7], "big")
            return (width, height) if width and height else None
        offset += size
    return None


def ingest_traces(connection: sqlite3.Connection, root: Path) -> dict:
    """Import bounded Dock and Finder outcomes as metadata-only local feedback."""
    journal = root / "steps.jsonl"
    if not journal.is_file() or journal.stat().st_size > 8_000_000:
        raise ValueError("bounded local trace journal required")
    imported = skipped = locators_added = source_apps_added = 0
    reasons = Counter()
    with journal.open(encoding="utf-8") as handle:
        for line in handle:
            event = json.loads(line, object_pairs_hook=unique_pairs)
            if type(event) is not dict:
                skipped += 1
                reasons["invalid_row"] += 1
                continue
            identity = event.get("id")
            target = event.get("target_app")
            target_view = event.get("target_view")
            dock = (type(target) is str and target in DOCK_SCOPES
                    and not target_view and event.get("action_source") in
                    (None, "model_tooltip_guarded", "offline_locator_tooltip_guarded")
                    and event.get("active_app_before") not in (None, target))
            sidebar_source = event.get("action_source")
            sidebar_guarded = (sidebar_source in
                               ("model_label_guarded", "offline_policy_label_guarded")
                               or (sidebar_source == "model"
                                   and event.get("proposal_in_hit_region") is True)
                               or (sidebar_source == "ocr"
                                   and event.get("corrected_by_ocr") is True))
            sidebar = (target == "Finder" and type(target_view) is str
                       and target_view in SIDEBAR_VIEWS and sidebar_guarded
                       and event.get("active_app_before") == "Finder")
            if (type(identity) is not str or not re.fullmatch(r"[0-9a-f]{32}", identity)
                    or not (dock or sidebar)
                    or event.get("action") != "click"
                    or event.get("executed") is not True or type(event.get("verified")) is not bool):
                skipped += 1
                reasons["not_eligible_action"] += 1
                continue
            already = connection.execute("SELECT 1 FROM feedback WHERE review_id=?", (identity,)).fetchone() is not None
            has_locator = connection.execute("SELECT 1 FROM locators WHERE review_id=?", (identity,)).fetchone() is not None
            has_source = connection.execute("SELECT 1 FROM source_apps WHERE review_id=?", (identity,)).fetchone() is not None
            source_app = event.get("active_app_before") if dock and event.get("active_app_before") in SOURCE_APPS else None
            needs_locator = dock and event["verified"] is True and not has_locator
            needs_source = source_app is not None and not has_source
            if already and not (needs_locator or needs_source):
                skipped += 1
                reasons["already_imported"] += 1
                continue
            before = root / "frames" / f"{identity}.jpg"
            after = root / "frames" / f"{identity}-after.jpg"
            if (event.get("frame") != f"frames/{identity}.jpg"
                    or event.get("after_frame") != f"frames/{identity}-after.jpg"
                    or not before.is_file() or not after.is_file()
                    or before.stat().st_size > 4_000_000 or after.stat().st_size > 4_000_000):
                skipped += 1
                reasons["missing_or_large_frame"] += 1
                continue
            before_data = before.read_bytes()
            after_data = after.read_bytes()
            if (hashlib.sha256(before_data).hexdigest() != event.get("frame_sha256")
                    or hashlib.sha256(after_data).hexdigest() != event.get("after_sha256")):
                skipped += 1
                reasons["frame_hash_mismatch"] += 1
                continue
            dimensions = jpeg_dimensions(before_data) if dock else None
            point = event.get("coordinates") if dock else None
            valid_dock_point = (dimensions is not None and type(point) is dict
                                and type(point.get("x")) in (int, float)
                                and type(point.get("y")) in (int, float)
                                and 0 <= point["x"] < dimensions[0]
                                and dimensions[1] * 0.8 <= point["y"] < dimensions[1])
            if dock and event.get("action_source") is None:
                if not valid_dock_point:
                    skipped += 1
                    reasons["old_click_not_in_dock"] += 1
                    continue
            context = ({"scope": {"kind": "app", "id": DOCK_SCOPES[target], "version": "dock.v1"},
                        "goal": "app.frontmost", "facts": ["app.not_frontmost"]} if dock else
                       {"scope": {"kind": "app", "id": "com.apple.finder", "version": "sidebar.v1"},
                        "goal": f"finder.sidebar.{target_view.lower()}.visible",
                        "facts": ["finder.active", "view.not_visible"]})
            action = "dock_app_click" if dock else "finder_sidebar_click"
            verifier_state = "app.frontmost" if dock else f"finder.sidebar.{target_view.lower()}.visible"
            feedback = {"schema": SCHEMA,
                                 "context": context, "action": action,
                                 "outcome": "verified_success" if event["verified"] else "verified_failure",
                                 "verifier": {"kind": "ocr", "state": verifier_state, "independent": True},
                                 "review_id": identity,
                                 "rights": {"ownership": "local-use", "training": "local-only",
                                            "consent_id": "user.local"}}
            if dock and event["verified"] and valid_dock_point:
                feedback["locator"] = {"x": point["x"] / dimensions[0],
                                       "y": point["y"] / dimensions[1]}
            if source_app is not None:
                feedback["source_app"] = source_app
            if already:
                changed = False
                with connection:
                    if "locator" in feedback and not has_locator:
                        connection.execute("INSERT INTO locators VALUES (?,?,?,?,?)",
                                           (identity, context_key(context), action,
                                            feedback["locator"]["x"], feedback["locator"]["y"]))
                        locators_added += 1
                        changed = True
                    if source_app is not None and not has_source:
                        connection.execute("INSERT INTO source_apps VALUES (?,?)", (identity, source_app))
                        source_apps_added += 1
                        changed = True
                if not changed:
                    skipped += 1
                    reasons["already_imported"] += 1
            else:
                observe(connection, feedback)
                imported += 1
    return {"offline_ingest": "complete", "imported": imported, "skipped": skipped,
            "locators_added": locators_added, "source_apps_added": source_apps_added,
            "skip_reasons": dict(reasons),
            "feedback_rows": connection.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]}


def export_local(connection: sqlite3.Connection, path: Path) -> dict:
    rows = []
    for review_id, context_json, action, outcome, x, y, source_app in connection.execute(
            "SELECT f.review_id,c.context_json,f.action,f.outcome,l.x,l.y,s.source_app FROM feedback f "
            "JOIN contexts c ON c.context_key=f.context_key "
            "LEFT JOIN locators l ON l.review_id=f.review_id "
            "LEFT JOIN source_apps s ON s.review_id=f.review_id ORDER BY f.review_id"):
        row = {"review_id": review_id, "context": json.loads(context_json),
               "action": action,
               "outcome": "verified_success" if outcome else "verified_failure"}
        if x is not None and y is not None:
            row["locator"] = {"x": x, "y": y}
        if source_app is not None:
            row["source_app"] = source_app
        rows.append(row)
    payload = json.dumps({"schema": "virtualos.local-feedback/v1", "usage": "local-only",
                          "examples": rows}, sort_keys=True, separators=(",", ":")).encode()
    if len(payload) > 1_048_576:
        raise ValueError("local export exceeds 1 MiB")
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
    return {"offline_dataset": "written", "examples": len(rows),
            "bytes": len(payload), "usage": "local-only", "path": str(path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("observe", "suggest", "stats", "ingest-traces", "export-local", "next-finder-target", "next-visible-finder-target"))
    parser.add_argument("request", nargs="?")
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command not in {"stats", "export-local", "next-finder-target", "next-visible-finder-target"} and not args.request:
            raise ValueError("request file required")
        if args.command in {"stats", "export-local", "next-finder-target", "next-visible-finder-target"} and args.request:
            raise ValueError("this command accepts no request")
        connection = open_db(args.db)
        if args.command == "stats":
            result = {"feedback_rows": connection.execute("SELECT COUNT(*) FROM feedback").fetchone()[0],
                      "db_bytes": args.db.stat().st_size,
                      "source_app_rows": connection.execute("SELECT COUNT(*) FROM source_apps").fetchone()[0],
                      "locator_samples": connection.execute("SELECT COUNT(*) FROM locators").fetchone()[0],
                      "scope_contexts": connection.execute("SELECT COUNT(DISTINCT context_key) FROM feedback").fetchone()[0]}
        elif args.command == "export-local":
            result = export_local(connection, args.db.parent / "dataset.json")
        elif args.command == "next-finder-target":
            result = next_finder_target(connection)
        elif args.command == "next-visible-finder-target":
            from computer_use import GuestVM, _active_guest_app, _finder_sidebar_label
            frame = GuestVM().frame()
            if _active_guest_app(frame) == "Finder":
                visible = {view for view in SIDEBAR_VIEWS
                           if _finder_sidebar_label(frame, view) is not None}
                result = next_finder_target(connection, visible)
                result["visible_target_count"] = len(visible)
            else:
                result = {"target": "Applications", "reason": "finder_activation_seed",
                          "visible_target_count": 0}
        elif args.command == "ingest-traces":
            result = ingest_traces(connection, Path(args.request))
        else:
            request = read_request(Path(args.request))
            result = observe(connection, request) if args.command == "observe" else suggest(connection, request)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, RuntimeError, sqlite3.Error, json.JSONDecodeError) as error:
        print(f"offline_policy=refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
