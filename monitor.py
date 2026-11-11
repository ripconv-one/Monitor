"""Website uptime monitor using only Python's standard library."""

import argparse
import csv
import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

LOG = logging.getLogger("uptime")


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    sites = config.get("sites")

    if not isinstance(sites, list) or not sites:
        raise ValueError("sites must be a nonempty list")

    names = set()

    for site in sites:
        if not isinstance(site, dict):
            raise ValueError("Each site must be an object")

        name = site.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("Site names must be nonempty and unique")
        names.add(name)

        parsed = urlsplit(site.get("url", ""))
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "URLs must use HTTP(S) without embedded credentials"
            )

        codes = site.setdefault("expected_status", [200])
        if (
            not isinstance(codes, list)
            or not codes
            or any(
                type(code) is not int or not 100 <= code <= 599
                for code in codes
            )
        ):
            raise ValueError(
                "expected_status must contain HTTP status integers"
            )

    for key, default in (
        ("interval_seconds", 60),
        ("timeout_seconds", 10),
    ):
        value = config.setdefault(key, default)
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{key} must be positive and finite")

    threshold = config.setdefault("failure_threshold", 2)
    if type(threshold) is not int or threshold < 1:
        raise ValueError("failure_threshold must be a positive integer")

    return config


def connect(path):
    db = sqlite3.connect(path)
    db.execute("""
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY,
            checked_at TEXT NOT NULL,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            status INTEGER,
            latency_ms REAL NOT NULL,
            ok INTEGER NOT NULL,
            error TEXT
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS checks_target
        ON checks(name, url, id)
    """)
    return db


def probe(site, timeout):
    start = time.monotonic()
    status = None
    error = None

    try:
        request = Request(
            site["url"],
            headers={"User-Agent": "PythonUptimeMonitor/1.0"},
        )
        with urlopen(request, timeout=timeout) as response:
            status = response.status
    except HTTPError as exc:
        status = exc.code
        exc.close()
    except (URLError, OSError, ValueError) as exc:
        error = str(exc)

    ok = status in site.get("expected_status", [200]) and error is None

    if not ok and error is None:
        error = f"Unexpected HTTP status: {status}"

    return (
        datetime.now(timezone.utc).isoformat(),
        site["name"],
        site["url"],
        status,
        round((time.monotonic() - start) * 1000, 2),
        int(ok),
        error,
    )


def previous_failures(db, site, threshold):
    rows = db.execute(
        """
        SELECT ok FROM checks
        WHERE name = ? AND url = ?
        ORDER BY id DESC LIMIT ?
        """,
        (site["name"], site["url"], threshold),
    ).fetchall()

    failures = 0
    for (ok,) in rows:
        if ok:
            break
        failures += 1

    return failures


def notify(event, site, result, timeout):
    LOG.warning(
        "%s: %s (%s)",
        event,
        site["name"],
        result[6] or "reachable",
    )

    webhook = os.environ.get("UPTIME_WEBHOOK_URL")
    if not webhook:
        return

    payload = json.dumps({
        "event": event,
        "site": site["name"],
        "url": site["url"],
        "checked_at": result[0],
        "status": result[3],
        "latency_ms": result[4],
        "error": result[6],
    }).encode("utf-8")

    request = Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urlopen(request, timeout=timeout):
            pass
    except (URLError, OSError, ValueError) as exc:
        LOG.error("Webhook delivery failed: %s", exc)


def check_all(db, config):
    all_ok = True
    threshold = config["failure_threshold"]

    for site in config["sites"]:
        failures = previous_failures(db, site, threshold)
        result = probe(site, config["timeout_seconds"])

        with db:
            db.execute(
                """
                INSERT INTO checks
                    (checked_at, name, url, status, latency_ms, ok, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                result,
            )

        ok = bool(result[5])
        all_ok = all_ok and ok

        LOG.info(
            "%s %s status=%s latency=%sms",
            site["name"],
            "UP" if ok else "DOWN",
            result[3],
            result[4],
        )

        if not ok and failures == threshold - 1:
            notify("DOWN", site, result, config["timeout_seconds"])
        elif ok and failures >= threshold:
            notify("RECOVERED", site, result, config["timeout_seconds"])

    return all_ok


def report(db):
    rows = db.execute("""
        SELECT
            name,
            url,
            COUNT(*),
            ROUND(100.0 * SUM(ok) / COUNT(*), 2),
            ROUND(AVG(latency_ms), 2)
        FROM checks
        GROUP BY name, url
        ORDER BY name
    """)

    print("SITE\tURL\tCHECKS\tSUCCESS %\tAVG MS")
    for row in rows:
        print("\t".join(map(str, row)))


def export_csv(db, destination):
    cursor = db.execute("SELECT * FROM checks ORDER BY id")

    with open(destination, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([column[0] for column in cursor.description])
        writer.writerows(cursor)

    print(f"Check history exported to {destination}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=["check", "watch", "report", "export"],
    )
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--db", default="uptime.db")
    parser.add_argument(
        "--output",
        default=None,
        help="Export check history to this CSV file",
    )
    args = parser.parse_args()

    if args.output is not None and args.command not in ("report", "export"):
        parser.error("--output can only be used with report or export")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        config = (
            load_config(args.config)
            if args.command in ("check", "watch")
            else None
        )

        db = connect(args.db)
        try:
            if args.command == "report":
                report(db)
                if args.output is not None:
                    export_csv(db, args.output)

            elif args.command == "export":
                destination = (
                    args.output if args.output is not None else "checks.csv"
                )
                export_csv(db, destination)

            elif args.command == "check":
                return 0 if check_all(db, config) else 1

            else:
                while True:
                    check_all(db, config)
                    time.sleep(config["interval_seconds"])
        finally:
            db.close()

    except KeyboardInterrupt:
        LOG.info("Monitor stopped")
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        LOG.error("%s", exc)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())