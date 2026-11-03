#!/usr/bin/env python3
"""Send due supplemental alert context independently from immediate alerts."""
import datetime as dt
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('prw_alert', Path(__file__).with_name('proxmox-resource-alert.py'))
alert = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alert)

if __name__ == '__main__':
    now = dt.datetime.now().astimezone()
    alert.followups.run(alert.reporter, alert.LOG_DIR/'followups', lambda: alert.recent(now), now)
