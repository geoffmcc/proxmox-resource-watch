# Changelog

## [0.1.0] - Unreleased (public preview)

- Samples host CPU, process activity, available GPU counters, temperature sensors and fans.
- Stores dated JSONL logs, compresses older dates, and retains 30 days by default.
- Sends daily plain-text summaries with Celsius/Fahrenheit values and process correlations.
- Checks CPU temperature and sustained CPU load for warning/critical emails with cooldowns.
- Includes a conservative installer, systemd units, and regression tests.

This version is a preview. Validate notifications and sensor labels on your own hardware.
