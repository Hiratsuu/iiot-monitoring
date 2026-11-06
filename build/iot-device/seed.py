#!/usr/bin/env python3
"""Initialise the SQLite database (schema + baseline data)."""

import argparse
import sys

from iot_device import create_app
from iot_device.seed_data import seed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-run baseline inserts even when the database is already seeded"
        " (missing rows only; existing rows are kept)",
    )
    args = parser.parse_args(argv)

    app = create_app(seed_on_start=False)
    if seed(app, force=args.force):
        print(f"seeded database at {app.config['DB_PATH']}")
    else:
        print(
            f"database already seeded at {app.config['DB_PATH']}"
            " (use --force to re-add missing baseline rows)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
