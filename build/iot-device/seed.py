#!/usr/bin/env python3
"""Initialise the SQLite database (schema + baseline data)."""

import sys

from iot_device import create_app
from iot_device.seed_data import seed


def main() -> int:
    app = create_app()
    seed(app)
    print(f"seeded database at {app.config['DB_PATH']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
