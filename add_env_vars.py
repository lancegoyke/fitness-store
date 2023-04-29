#!/usr/bin/env python3
"""Send `.env.prod` to Heroku through the Heroku CLI."""

import subprocess

with open(".env.prod", "r") as env_file:
    for line in env_file:
        key, value = line.split("=")
        subprocess.call(
            [
                "heroku",
                "config:set",
                f"{key}={value.strip()}",
                "-a",
                "mastering-fitness",
            ]
        )
