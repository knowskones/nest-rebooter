# Nest Rebooter (Portable)

Automated scheduled restart for Google Nest WiFi / Google WiFi using a single cross-platform Python script.

---

## Credit

Based on the original project:

joep1000 / nest-rebooter  
https://github.com/joep1000/nest-rebooter

The core API interaction and authentication model originate from that implementation.  
This version is a complete rewrite focused on portability, usability, and operational reliability.

---

## Overview

`nest_rebooter_portable.py` is a single-file Python script that automates restarting a Google Nest WiFi or Google WiFi network using Google's backend API.

It is designed for unattended execution and runs on:

- Windows
- macOS
- Linux

No services, daemons, or background processes are required.

---

## Problem

Nest WiFi and Google WiFi networks often degrade over time:

- Reduced throughput after extended uptime
- Increased latency and instability
- Mesh performance degradation

A manual reboot resolves this, but Google does not provide scheduling.

---

## Solution

This script enables:

- Reliable automated network reboot
- Single-file deployment
- Cross-platform scheduling
- No ongoing user interaction

---

## Core Functionality

- Full network restart (router and access points)
- Uses Google Home backend API
- One-time authentication
- Unattended scheduled execution
- Local logging and history tracking

---

## Key Features (v3.1.3)

### Execution

- No arguments required for normal operation
- Safe for direct use in schedulers
- Built-in CLI for setup, testing, and diagnostics

### Reliability

- PID-based lock file (prevents concurrent runs)
- HTTP retry with exponential backoff
- Authentication retry handling
- Access token caching per run
- Atomic configuration writes

### Safety

- SSID validation before reboot
- Failure if SSID cannot be determined
- Dry-run support
- Token redaction in logs

### User Experience

- Clear setup prompts with context
- Progress output for long-running operations
- Countdown timers for waits and delays
- ASCII startup banner

### Scheduling

- Native OS scheduler integration:
  - Windows Task Scheduler
  - macOS launchd
  - Linux cron
- Scheduler status reflects actual OS state

### Optional Features

- Experimental Google/Nest speed test integration
- Pre and post reboot performance logging

---

## How It Works

```
Browser login
    ↓
oauth_token
    ↓
gpsoauth.exchange_token()
    ↓
Master token
    ↓
gpsoauth.perform_oauth()
    ↓
Access token
    ↓
POST /v2/groups/{system_id}/reboot
```

---

## Requirements

- Python 3.8+
- Internet connection
- Google account that owns the network
- Local browser (setup only)

---

## Installation

Run:

```bash
python nest_rebooter_portable.py
```

## Setup

Run:

```bash
python nest_rebooter_portable.py
```

### Setup Flow

1. Dependencies installed (if required)
2. Browser opens for Google login
3. Authentication token captured automatically
4. Network autodiscovery attempted
5. Configuration written locally

---

## Token Storage

During setup you will be prompted to save the authentication token.

### If saved

- Required for scheduled unattended execution
- Stored in `config.json`

### If not saved

- Script works for current run only
- Cannot be scheduled
- Setup must be repeated on next run

---

## Security Considerations

- No password is stored
- A long-lived token may be stored in `config.json`
- Anyone with access to the script folder could potentially reuse the token

Recommended:

- Keep the script directory private
- Do not commit to version control
- Do not share config or log files

---

## Files Created

All files are stored alongside the script:

```
config.json
nest-rebooter.log
nest-rebooter.lock
android_id.txt
browser-profile/
speedtest-history.jsonl (optional)
run-history.jsonl (optional)
```

---

## Usage

### Default (reboot)

```bash
python nest_rebooter_portable.py
```

---

## Scheduling

The script runs without arguments and is safe to execute directly.

---

### Windows

**Program:**

```
py
```

**Arguments:**

```
"C:\path\to\nest_rebooter_portable.py"
```

---

### Linux

```bash
crontab -e
```

Example:

```bash
0 3 * * * /usr/bin/python3 /path/nest_rebooter_portable.py
```

---

### macOS

Automatically configured via launchd during setup.

Manual cron alternative:

```bash
0 3 * * * /usr/bin/python3 /path/nest_rebooter_portable.py
```

---

## Safety Model

| Scenario | Behaviour |
|--------|----------|
| Wrong SSID detected | Execution stops |
| SSID cannot be detected | Execution stops |
| Multiple instances | Blocked by lock file |
| Network not recovered | Logged with warning |
| API failure | Retry with backoff |

---

## Behaviour Notes

- The script intentionally fails closed when network identity cannot be verified
- This prevents rebooting unintended networks
- Scheduled execution assumes a stable environment (same machine, same network)

---

## Advanced Usage

### Test authentication

```bash
python nest_rebooter_portable.py test
```

---

### Status

```bash
python nest_rebooter_portable.py status
```

Full output:

```bash
python nest_rebooter_portable.py status --full
```

---

### Speed test

```bash
python nest_rebooter_portable.py speedtest
```

Options:

```bash
--phase manual
--phase pre-reboot
--phase post-reboot
```

---

### Install dependencies

```bash
python nest_rebooter_portable.py install-deps
```

---

### Dry run

```bash
NEST_REBOOTER_DRY_RUN=1 python nest_rebooter_portable.py
```

No reboot request will be sent.

---

### Skip dependency check

```bash
--skip-dependency-check
```

---

### Disable autodiscovery

```bash
--no-discover
```

---

### Manual token input

```bash
--manual-cookie
```

---

### Set schedule during setup

```bash
--schedule-time HH:MM
```

---

### Token storage flags

```bash
--save-token
--no-save-token
```

---

## Logging

Primary log file:

```
nest-rebooter.log
```

Includes:

- execution flow
- retry events
- network checks
- API responses

Sensitive values are automatically redacted.

---

## Limitations

- Uses an undocumented Google API
- Behaviour may change without notice
- Requires browser login during setup
- Speed test functionality is not guaranteed

---

## Troubleshooting

### Script appears inactive

The script logs progress during:

- dependency installation
- browser login wait
- network recovery checks
- reboot delays

Check the log file for activity.

---

### SSID not detected

- Ensure system is connected via WiFi
- Run setup again from the target network
- Provide SSID manually if required

---

### Scheduler not running

Validate:

```bash
python nest_rebooter_portable.py schedule-status
```

---

## Summary

This implementation provides:

- A portable, single-file solution
- Reliable unattended scheduled execution
- Strict safety controls
- Clear operational visibility

It is suitable for long-term automated operation in a controlled environment.

---

## Extendability

The current architecture supports:

- Multi-network management
- External alerting (email, webhook)
- Performance tracking integrations
- Packaging into standalone executable
```
