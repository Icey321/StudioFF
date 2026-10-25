# StudioFF

A desktop app for the [FaceFusion v4](https://github.com/facefusion/facefusion/tree/v4) face swapper. Not made or endorsed by the FaceFusion team.

StudioFF swaps faces in images, videos and GIFs. Pick the faces to use, pick a target, preview
the swap on one frame and run it, one target at a time or a whole folder in a batch. Several people in one
target can each get a different face.

It is a Windows 11 desktop app for FaceFusion v4, which has no built-in UI of its own, only a command line.
StudioFF builds the command for you, runs it in FaceFusion's own Python environment and shows
progress and the result.

FaceFusion itself is not included: install FaceFusion v4 separately, then point StudioFF at it.

## Screenshots

| Before | After |
| --- | --- |
| ![Before: target with mapped faces](docs/images/before.webp) | ![After: result with the faces swapped](docs/images/after.webp) |
| **Batch** | **Setup** |
| ![Batch: every source face against every target, with a live preview](docs/images/batch.webp) | ![Setup: FaceFusion location, install, updates and appearance](docs/images/setup.webp) |

## Features

- **Studio**: swap faces in one target. Drop in source faces and a target image, video or GIF, turn on processors and drag them into order. The preview tries your settings on one frame (automatically with **Auto**), and **Start** runs the full job. Models stay loaded between previews, so after the first one each takes about 2 seconds. Setup's **Preview models** setting picks how long they stay loaded after the last preview (5 minutes by default), and Start, Batch and Benchmark free them first.
- **Batch**: every source face × every target in a folder, with its own settings (or copied from Studio in one click). Each planned output shows thumbnails and can be removed (removed outputs stay out after a restart until you restore them). A live preview tries the selected one, on the frame you pick for a video (remembered after a restart), and one failed target doesn't stop the rest. Copy Settings from Studio asks first when it would replace settings that differ.
- **Face mapping**: give each person in the target their own source face. Two people can even swap faces with each other.
- **GIF support**: GIF targets are converted for FaceFusion and saved back as looping GIFs (needs `ffmpeg`).
- **Output names**: build file names from tags like `{source}`, `{target}`, `{date}` and `{n}`, with an optional sub-folder per source.
- **Favorite Settings and search**: star any setting to pin it to Favorite Settings, or filter all settings with *Search settings*. Open or closed setting groups stay that way.
- **Faces**: shows the faces FaceFusion detects in the current frame and which ones will be processed. If none will be, it says why.
- **Mask**: shades the face mask FaceFusion will blend through, and redraws as you change mask settings.
- **Models**: download the lite or full model set, see what's installed, and delete models to free space.
- **Benchmark**: runs FaceFusion's benchmark on your hardware and exports the results as a shareable image.
- **Setup**: point the app at an existing FaceFusion v4 or install it from scratch, and check GitHub for FaceFusion updates. It shows whether your FaceFusion is the version StudioFF was tested with, and can switch to that version, to the latest one or back again.
- **System monitor**: graphics card, processor and memory use in the title bar, read with FaceFusion's own libraries. It can be turned off in Setup.
- **Custom look**: Auto, Light or Dark theme, with your own accent color for each (one click puts back the standard purple).
- **Fluent design**: Windows 11 style throughout, a splash screen with a loading bar, and settings remembered between runs.

## Install

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

This creates `.venv` for the UI (PySide6 + PySide6-Fluent-Widgets + OpenCV) and adds desktop and Start menu
shortcuts. Then start **StudioFF** and open **Setup**:

- **Already have FaceFusion v4?** Pick its folder and the `python.exe` of its environment, or press **Detect**, then press **Save and load options**.
- **No FaceFusion v4 yet?** Use the **Install FaceFusion v4** button in the app's **Setup** page. Note that FaceFusion v4 requires **Python 3.13+**, so a
  FaceFusion 3.x environment (Python 3.12) can't run it.

FaceFusion also needs `ffmpeg`, `ffprobe` and `curl` on `PATH`. Setup checks for them.

## Layout

| Path | Purpose |
| --- | --- |
| `ffstudio/schema_dump.py` | Runs inside the FaceFusion environment and dumps the `run` options as JSON |
| `ffstudio/schema.py` | Loads that schema, resolves model-dependent choices, builds CLI flags |
| `ffstudio/output_name.py` | File name tags (`{target}`, `{source}`, `{n}` …) and the output path they build |
| `ffstudio/runner.py` | `QProcess` wrapper that parses FaceFusion's progress bar output |
| `ffstudio/session.py` | Shared state: config, schema, option values, runner |
| `ffstudio/benchmark_report.py` | Draws the exported benchmark image (hardware, settings, results) |
| `ffstudio/gif_run.py` | Runs inside the FaceFusion environment: GIF → video → FaceFusion → GIF |
| `ffstudio/content_check.py` | Runs inside the FaceFusion environment: FaceFusion's NSFW filter on targets, without processing |
| `ffstudio/nsfw.py` | NSFW messages, the red bar and the flagged-frames window |
| `ffstudio/face_probe.py` | Runs inside the FaceFusion environment and stays running: the faces found in a frame, which would be processed, and their face mask |
| `ffstudio/faces.py` | Talks to `face_probe.py` and words its results |
| `ffstudio/system_monitor.py` | Runs inside the FaceFusion environment and stays running: graphics card, processor and memory use once a second |
| `ffstudio/monitor.py` | Talks to `system_monitor.py` and shows its numbers in the title bar |
| `ffstudio/pages/*` | Studio, Batch, Models, Benchmark, Console, Setup |

App data (settings, cached schema, preview frames) lives in `%APPDATA%\StudioFF` (versions before v0.40 used `%APPDATA%\FaceFusionStudio`,
which is moved over on the first start). The preview
folder only ever holds the latest frame and result (overwritten each time) and is deleted when the app
closes. After **Stop** or a failed run, FaceFusion's temp folder for this machine is cleared too.
Set `FFSTUDIO_DATA_DIR` to use a different folder.

## AI tools used

Most of the code was written with [Claude Code](https://claude.com/claude-code) (Anthropic's Claude
models), working from my requests and feedback. Changes were tested in the app before each release.
No other AI tools were used.

## License

Released into the public domain under [The Unlicense](UNLICENSE). FaceFusion has its own license.
