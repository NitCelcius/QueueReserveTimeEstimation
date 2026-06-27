# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Monitors the queue length of a reservation app via ADB screencap + OCR, and notifies when to submit a reservation so that seating at a target time is achieved. Uses a Little's law approximation: `wait = N × τ / tables`, where τ (avg turnover time) is calibrated from real observations.

## Commands

```bash
# First-time: calibrate tap position and OCR crop box (requires adb-connected Android device)
uv run python configure_regions.py

# Run the monitor (target time required)
uv run python main.py --target-time 19:00

# With explicit initial parameters
uv run python main.py --target-time 19:00 --avg-stay-min 32 --tables 30 --tol 5

# Dry-run logic check (no device needed — synthetic queue curve)
uv run python main.py --target-time 19:00 --simulate

# Syntax-check a file
uv run python -m py_compile main.py
```

Runtime commands (typed into stdin while `main.py` is running — see `HELP_TEXT` in `main.py`):
- `calibrate <N> <minutes>` — back-calculates τ from a real observation
- `avg_stay_min <min>` / `tables <n>` / `target HH:MM` / `tol <min>` / `poll_interval <sec>` — adjust parameters live
- `tap <x> <y>` / `crop <x1> <y1> <x2> <y2>` — override calibration on the fly
- `reload` — force an immediate re-read

## Architecture

### Data flow

```
Android screen  →  adb screencap  →  PIL crop  →  pytesseract OCR  →  queue count N
                                                                         ↓
                                                               history: List[Sample]
                                                                         ↓
                                                            adaptive_slope (multi-window)
                                                                         ↓
                                                              evaluate() → Estimate
                                                                         ↓
                                                              is_hit? → notify + exit
```

### Key design points

**`Config` dataclass (`main.py:74`)** — all tuneable parameters live here; the stdin command thread mutates this object in place without restarts. Never copy fields out of `CFG` into local vars across a poll cycle — read directly so live updates take effect.

**`adaptive_slope` (`main.py:173`)** — tries window lengths `(10, 20, 40, 80)` minutes from shortest to longest. Picks the shortest window whose sign agrees with the next-wider window (handles plateau/reversal detection). Falls back to the longest window when no pair agrees.

**`evaluate` (`main.py:190`)** — projects N forward by `LOOKAHEAD_MIN` (1 min), converts to wait via Little's law, and computes projected seating time. Returns an `Estimate` dataclass; all display and decision logic consumes this one object.

**Calibration persistence** — `configure_regions.py` writes `calib.json` with `tap_pos` and `crop_box`. `main.py` reads this at startup via `load_calibration()`. Format: `{"tap_pos": [x, y], "crop_box": [x1, y1, x2, y2]}` in device pixel coordinates.

**OCR** — takes median of 3 reads per cycle to suppress noise. Tesseract path is hardcoded to `C:\Program Files\Tesseract-OCR\tesseract.exe`; change `pytesseract.pytesseract.tesseract_cmd` at `main.py:60` if needed.

**Threading model** — one background daemon thread (`command_listener`) reads stdin; main thread owns the poll loop. Communication is via direct `CFG` mutation (no locks — individual attribute writes are GIL-safe for simple types) and `_reload_event` (a `threading.Event`).

## Python Conventions

- After logic changes: `uv run python -m py_compile <file>` (a PostToolUse hook does this automatically on every edit)
