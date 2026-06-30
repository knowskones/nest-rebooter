# Nest-Rebooter (Portable Edition)

**Automated scheduled restart for Google Nest WiFi / Google WiFi — cross-platform, single-file implementation.**

---

## Credit

This project is based on the original work by:

**joep1000 / nest-rebooter**  
https://github.com/joep1000/nest-rebooter

The core concept, API method, and authentication approach originate from that project.  
This version is a complete rewrite focused on portability, simplicity, and production reliability.

---

## Overview

This script automates the restart of a Google Nest WiFi / Google WiFi network by calling the same Google cloud API used by the Google Home application.

It is implemented as a **single Python file** and runs on:

- Windows
- macOS
- Linux

The script is designed to run unattended via the native scheduler of each operating system.

---

## Problem

Google Nest WiFi and Google WiFi systems are known to degrade in performance over time:

- Throughput drops after 1–3 days
- Latency and consistency degrade
- Recovery typically requires manual restart

There is no native scheduling feature provided by Google.

This script provides a reliable automated workaround.

---

## Functionality

- Performs a full network restart (router and all access points)
- Uses official Google Home backend infrastructure
- Requires one-time authentication
- Runs unattended on a schedule
- Includes safety checks and logging
- Requires no additional services or background processes

---

## Key Adjustments

| Area | Original | Portable Version |
|------|----------|----------------|
| Platform | Linux-focused | Cross-platform |
| Installation | Bash installer | Single Python file |
| Scheduling | systemd | Native OS schedulers |
| Setup | Manual cookie extraction | Automated browser login |
| File layout | Multiple files | Self-contained |
| Logging | System directories | Local file |
| Execution | Requires arguments | Runs with no arguments |
| Reliability | Basic | Locking, validation, atomic writes |
| Safety | Minimal | SSID verification, dry-run |

---

## How It Works

The script uses the same backend API used by the Google Home app.

### Flow

```
User login (browser)
    ↓
OAuth cookie (oauth_token)
    ↓
gpsoauth.exchange_token()
    ↓
Master token (stored locally)
    ↓
gpsoauth.perform_oauth()
    ↓
Access token (generated per run)
    ↓
Google Home API
    ↓
Network reboot
```

### API Endpoint

```
POST https://googlehomefoyer-pa.googleapis.com/v2/groups/{system_id}/reboot
```

---

## Requirements

- Python 3.8 or newer
- Google account that **owns the WiFi network**
- Internet connection
- Local browser (for initial setup only)

---

## Installation

Install required dependencies:

```bash
pip install gpsoauth playwright
python -m playwright install chromium
```

No further installation is required.

---

## Setup

Run once:

```bash
python nest_rebooter_portable_v3.py setup
```

### Setup Process

1. A browser window opens automatically
2. Log in with your Google Home account
3. Script captures authentication token
4. Script attempts to identify your network
5. Configuration is saved locally

### Files Created

All files are stored in the same directory as the script:

```
config.json
nest-rebooter.log
nest-rebooter.lock
android_id.txt
browser-profile/
```

---

## Usage

### Normal execution

```bash
python nest_rebooter_portable_v3.py
```

- No arguments required
- Default behaviour is a network reboot

---

### Test without reboot

```bash
python nest_rebooter_portable_v3.py test
```

---

### View status

```bash
python nest_rebooter_portable_v3.py status
```

---

### Dry run

```bash
NEST_REBOOTER_DRY_RUN=1 python nest_rebooter_portable_v3.py
```

No network action is performed.

---

## Scheduling

### Windows (Task Scheduler)

**Program:**

```text
py
```

**Arguments:**

```text
"C:\path\to\nest_rebooter_portable_v3.py"
```

No additional parameters required.

---

### Linux (cron)

```bash
crontab -e
```

Example:

```bash
0 3 * * * /usr/bin/python3 /path/nest_rebooter_portable_v3.py
```

---

### macOS (cron or launchd)

Simple cron example:

```bash
0 3 * * * /usr/bin/python3 /path/nest_rebooter_portable_v3.py
```

---

## Safety Features

- Ensures only one instance runs at a time
- Optional check to confirm correct WiFi network before reboot
- Dry-run capability for testing
- Secure log output (tokens are redacted)
- Atomic configuration writes to prevent corruption

---

## Security Considerations

- The script does not store your Google password
- A long-lived authentication token is stored locally
- Protect the script directory with appropriate filesystem permissions

---

## Limitations

- Uses an undocumented Google API
- May break if Google changes backend behaviour
- Requires initial browser authentication
- Autodiscovery may require manual confirmation in some cases

---

## Summary

This version provides:

- A portable implementation of the original concept
- Reliable unattended execution across all operating systems
- Simplified setup and operation
- Production-grade safeguards for long-term use

---

If further extension is required, this architecture supports:

- Post-reboot validation (speed test)
- Multi-network support
- Notification integrations
- Packaging as a standalone executable
