# Notes on `nysh workspace export` progress logging

Observed during a real **17.7 GB / 573,805-file** export from an Intel MacBook Pro to an APFS `.sparsebundle` on an exFAT USB drive (`local/mps-transfer` @ `377bdc69`).

## What happened

The export succeeded and verified all 573,805 files (`✅ пакунок готовий`), **but** the terminal output was ~350+ lines of progress spam:

1. **Scan phase (`_scan_section` in `src/nyshporka/setup/transfer.py`):**
   - `_SCAN_PROGRESS_SEC = 1.5` printed a new line every 1.5 seconds (~43 lines):
     ```text
     сканую простір (/Users/nataliiagodynska/Documents/Нишпорка)…
       … 9984 файлів (15.5 ГБ)
       … 22784 файлів (15.5 ГБ)
       …
       … 563200 файлів (17.7 ГБ)
     ```
2. **Copy phase (`mirror` in `src/nyshporka/setup/transfer.py`):**
   - `step = min(max(total // 20, 32 MiB), 100 MiB)` + `_COPY_PROGRESS_SEC = 2.0` printed a new line every ~100 MB **or** every 2 seconds.
   - During the first ~15 GB (large scans + `text_store.v4.sqlite`), throughput was ~75–80 MB/s, so it printed every ~1.3s.
   - During the last ~2.2 GB (`reports/htr/`, hundreds of thousands of tiny files), throughput dropped to ~19–30 MB/s, so the 2-second timer fired continuously for ~2 minutes (~200+ lines):
     ```text
     … 15.5 ГБ з 17.7 ГБ (87%, 73.9 МБ/с, ~32с)
     … 15.5 ГБ з 17.7 ГБ (87%, 73.3 МБ/с, ~32с)
     ...
     … 17.7 ГБ з 17.7 ГБ (99%, 18.8 МБ/с, ~0с)
     ✓ звірено 573805 файлів: скопійовано 573805 (17.7 ГБ)
     ```
   - Notice also that `15.5 ГБ` repeated ~20 times with the same rounded GB and percentage (`87%`) while copying small files.

## Where to change it

- `src/nyshporka/setup/transfer.py`:
  - `_SCAN_PROGRESS_SEC`, `_COPY_PROGRESS_SEC`, `_COPY_STEP_MIN`, `_COPY_STEP_MAX`
  - `_scan_section()` and `mirror()`
- `src/nyshporka/cli.py`:
  - `workspace_export()` (and `workspace_import()` if similar)

## Suggested fix

1. **Use an in-place Rich `Progress` / `Status` in `cli.py` when `console.is_terminal` is `True`** (updating a single line in place for scan file counts and copy bytes/speed/ETA, instead of appending hundreds of lines).
2. **For non-TTY / plain `echo` fallback** (pipes, logs, tests):
   - Increase intervals significantly (e.g., `_SCAN_PROGRESS_SEC = 10.0`, `_COPY_PROGRESS_SEC = 15.0`, and/or only emit when `pct` advances by >= 5% or `done_bytes` by >= 500 MB) so logs stay concise while still showing liveness.
3. Apply the fix on `feature/workspace-transfer` and merge back into `local/mps-transfer` (or `main` before opening the PR).
