"""Command line interface: `fitdays-sync auth` and `fitdays-sync sync`."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .client import (COLUMNS, LOGIN_WARNING, Credentials, FitdaysClient, FitdaysError,
                     default_session_file, measurement_rows, redact)


def _client(args) -> FitdaysClient:
    return FitdaysClient(Credentials.from_env(args.env_file), args.session_file)


def cmd_auth(args) -> None:
    client = _client(args)
    client.login()
    print(f"Logged in ({client.session['host']}). Session saved to {client.session_file}")
    print("The Fitdays app on your phone is now signed out: log out and back in there, "
          "or its weigh-ins will not upload.")


def merge_csv(path: Path, rows: dict[str, dict]) -> None:
    """Upsert weigh-ins by Fitdays data id, sorted by date and time."""
    existing: dict[str, dict] = {}
    if path.exists():
        with path.open(newline="") as f:
            existing = {r["data_id"]: r for r in csv.DictReader(f)}
    existing.update(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(existing.values(), key=lambda r: (r["date"], r["time"], r["data_id"])):
            writer.writerow(row)


def cmd_sync(args) -> None:
    data = _client(args).sync_from_server(args.days, login=args.login)
    if args.raw_dir:
        args.raw_dir.mkdir(parents=True, exist_ok=True)
        (args.raw_dir / "sync.json").write_text(json.dumps(redact(data), indent=2, ensure_ascii=False))
    rows = measurement_rows(data)
    merge_csv(args.csv, rows)
    print(f"{len(rows)} weigh-ins updated in {args.csv}.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fitdays-sync", description=__doc__)
    p.add_argument("--env-file", type=Path, default=Path(".env"),
                   help="file with FITDAYS_EMAIL / FITDAYS_PASSWORD (default: ./.env)")
    p.add_argument("--session-file", type=Path, default=default_session_file(),
                   help="where the login session is stored (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth", help="log in and save the session (signs the phone app out)")
    a.set_defaults(func=cmd_auth)

    s = sub.add_parser("sync", help="download recent weigh-ins into a CSV")
    s.add_argument("--days", type=int, default=30, help="how many days back to download (default: 30)")
    s.add_argument("--csv", type=Path, default=Path("fitdays.csv"),
                   help="CSV to update, one row per weigh-in (default: %(default)s)")
    s.add_argument("--raw-dir", type=Path,
                   help="also save the raw API response (without the account block) as JSON here")
    s.add_argument("--login", action="store_true",
                   help="log in if the saved session is missing or rejected. " + LOGIN_WARNING.split(". Run")[0] + ".")
    s.set_defaults(func=cmd_sync)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except FitdaysError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
