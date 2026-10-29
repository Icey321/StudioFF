# StudioFF (FaceFusion-NK) project rules

- Times shown to the user (how long a run or preview took) read as seconds under a minute
  ("42.3 s") and as minutes:seconds from 60 s on ("1:24"). Use `format_seconds` from
  `ffstudio/media.py`; a running clock uses `format_length`.
- A finished run's time shows once, as "Total m:ss" beside the progress bar, not again in
  the status line.
