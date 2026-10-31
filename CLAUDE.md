# StudioFF (FaceFusion-NK) project rules

- Any StudioFF test run from Claude (a test script, `--screenshot`, anything that builds
  `Session()` or `MainWindow`) MUST first set `FFSTUDIO_DATA_DIR` to a copy in the
  scratchpad. Nik's real settings (`%APPDATA%\StudioFF`) are NOT shielded from Claude: on
  2026-10-29 a test without it wiped his source faces, names and a face mapping.

- StudioFF supports only FaceFusion's v4 branch from commit e7a2848 (2026-10-30, `--face-selector-map`)
  on. Do not add or keep fallbacks for older FaceFusion commits (Nik, 2026-10-30).

- Nik works on this project only on his home PC, so the to-do list stays in Claude's
  memory (`deferred-features-oct-2026.md`), not in a file in the project.

- Times shown to the user (how long a run or preview took) read as seconds under a minute
  ("42.3 s") and as minutes:seconds from 60 s on ("1:24"). Use `format_seconds` from
  `ffstudio/media.py`; a running clock uses `format_length`.
- A finished run's time shows once, as "Total m:ss" beside the progress bar, not again in
  the status line.
