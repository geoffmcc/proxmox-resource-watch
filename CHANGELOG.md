# Changelog

## [0.2.0] - Unreleased

- Add an optional sandboxed LXC Jellyfin log snapshot helper and conservative support for loggerless task completions.

- Preserve clock-order uncertainty and sample discontinuity markers; never confirm historical recovery across uncertain ordering.
- Evaluate every retained recovery observation, including samples skipped by checker scheduling.
- Restore root ownership enforcement during installation and reject unsafe directory ownership at sampler startup.
- Reuse normalized rows and stream completed thermal events into bounded report selection.
- Accept structured Proxmox journal records and additional Jellyfin TaskManager formatting using sanitized fixtures.
- Log monitoring-health transitions without introducing a new mail dependency; test delivery retry after mail failure.

- Send minimal immediate alerts independently of optional diagnostics; reconstruct per-sensor events in daily reports.
- Reconstruct bounded Proxmox backup and optional Jellyfin maintenance intervals, distinguishing observed overlap from possible correlation and unknown causes.
- Enforce private 0700 log-directory permissions consistently during installation and sampler startup; preserve configuration and record contents.
- Merge conflicting duplicate timestamps conservatively, distinguish unknown recovery, separate hot intervals from recovery, and clip preceding process measurements to event boundaries.
- Restrict workload formats and confidence, bound and reuse report evidence, accept legacy local timestamps, and tolerate truncated gzip logs.
- Harden special-file handling, mail headers and atomic alert-state replacement; retain systemd restrictions.
- Expand offline regression, installer-policy and performance checks. v0.2.0 remains a preview pending independent review and deployment validation.

## [0.1.0] - Historical public preview

- Samples host CPU, process activity, available GPU counters, temperature sensors and fans.
- Stores dated JSONL logs, compresses older dates, and retains 30 days by default.
- Sends daily plain-text summaries with Celsius/Fahrenheit values and process correlations.
- Checks CPU temperature and sustained CPU load for warning/critical emails with cooldowns.
- Includes a conservative installer, systemd units, and regression tests.

This version is a preview. Validate notifications and sensor labels on your own hardware.
