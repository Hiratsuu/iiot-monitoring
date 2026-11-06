#!/usr/bin/env python3
"""Entry point for the IIoT Smart Controller baseline.

Runs the Flask development server on purpose (V7).  Bind address and port come
from /etc/iot-device/iot-device.env in the deployed unit.
"""

from iot_device import create_app

app = create_app()


if __name__ == "__main__":
    app.run(
        host=app.config["BIND_ADDR"],
        port=app.config["PORT"],
        threaded=True,
        debug=False,
    )
