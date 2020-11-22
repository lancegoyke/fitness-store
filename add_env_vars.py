#!/usr/bin/env python3
"""This script is meant to pull all the environment variables in `.env.prod`
and send them to heroku through the Heroku CLI.
"""
import subprocess


with open(".env.prod", "r") as env_file:
    for line in env_file:
        key, value = line.split("=")
        subprocess.call(
            ["heroku", "config:set", f"{key}={value}", "-a", "mastering-fitness"]
        )
