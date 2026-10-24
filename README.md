# FaceFusion Studio

A native Windows 11 desktop UI for [FaceFusion v4](https://github.com/facefusion/facefusion/tree/v4).

FaceFusion v4 has no built-in UI, only a command line and an API server. FaceFusion Studio drives the
`facefusion.py` command line for you: you pick sources, a target and processors, it builds the command,
runs it in FaceFusion's own Python environment and shows progress and the result.

FaceFusion itself is not included: install FaceFusion v4 separately, then point FaceFusion Studio at it.

## Features

- **Studio**: drag and drop source faces (or audio for lip sync) and a target image, video or GIF, scrub
  the video, set the reference frame and trim points, and switch on any of the 11 processors. Each frame
  goes through the processors left to right, so drag the processor buttons to change the order (the number
  on a button is its place in the run). **Preview Frame** processes just the current frame with your
  settings, and **Live** (the padlock half of the same button, closed while it is on) re-runs it by itself whenever a setting, source, processor or the frame changes.
  **Start** runs the full job with a live progress bar. *Result* shows a before/after slider that starts at
  the far left (all result): drag it right to reveal the target, double-click to jump between left and center.
  Clicking *Target* or *Result* again while it is selected flips to the other one, for quick A/B checks.
  *Preview Frame* and *Live* sit along the bottom of the Target card as one split button (both halves take the
  accent color while Live is on), next to the *Open Preview* (eye) and *Open Preview in Explorer* (folder) icons.
  While a preview runs, a progress bar shows there too. FaceFusion reports no
  progress for a single frame, so the bar is estimated from how long your previews took before.
  Click a source thumbnail to use only that one, Ctrl+click to add or remove one,
  Shift+click to pick a range (images and audio are picked separately). Click the name under a thumbnail to
  rename it; `{source}` in the file name uses that name. The *File name* box takes tags such as `{target}`,
  `{source}`, `{model}`, `{date}` and `{n}`: the tag icon inside the box lists them all with clickable examples
  (the folder icon inside the *Output* box picks the folder), and the *Saves As:* line under the boxes shows the
  file name you'll get. Switch on **Sub-Folders** to save into `<output folder>\<source name>\Images` or
  `\Videos` (picked from the result's type); the folders are made when the run starts. Each setting's description sits behind the faint (i) next
  to its name: hover over it or click it to read it. Hover over any setting and click the star left of its name to keep a copy
  of it in the **Favorites** box under *Processors*. Drag the divider between the two columns to resize them, and use
  *Search settings* to filter options (Setup has the same search). Number boxes and sliders only react to the
  mouse wheel after you click them. GIF targets are converted to video for FaceFusion, which has no GIF
  support, and the result is written back as a looping GIF (needs `ffmpeg` on PATH).
- **Options stay in sync with v4**: every option card comes from FaceFusion's own argument parser
  (`ffstudio/schema_dump.py`), including choices that depend on the selected model (detector size, pixel
  boost). Changed options are marked with a dot and can be reset. Only changed values are passed on the
  command line, so `facefusion.ini` defaults still apply.
- **Batch**: a folder of faces × a folder of targets, with a file name template and a list of planned
  outputs. Each output is its own FaceFusion run, so a target that fails or is refused only loses its own
  output and the batch carries on (FaceFusion's `batch-run` deletes every finished output when one step fails).
  The list marks each output as done (✓), refused by the NSFW filter (⛔) or failed (✗).
- **NSFW filter messages**: FaceFusion refuses targets its built-in NSFW filter flags, usually without
  saying why. Studio and Batch now say *NSFW content detected* in a red bar that stays until you close it.
  **NSFW Check** (next to Stop) runs FaceFusion's own filter on the target, or on every Batch target,
  without processing anything, so you know before a long run. Studio also runs it once by itself on each
  target you load (after anything already running), quietly when nothing is flagged and with the red bar
  when it is. The *NSFW content detected* line under the preview clears when the target changes or a
  check finds nothing. A flagged result from the button opens a window with the
  flagged image or frames; clicking a frame shows it on Studio's timeline. Batch skips targets the check
  flagged, and double-clicking a ⛔ output shows what was flagged. The filter itself is FaceFusion's and is
  left unchanged (`ffstudio/content_check.py` only reads the targets).
- **Faces** (Target card): shows the faces FaceFusion detects in the current frame with your face detector,
  aligner and selector settings. Outlined faces will be processed, dashed ones are skipped by the face selector.
  Each tag shows the detector score, gender and age, plus the distance to the reference face in reference
  mode. When no face gets processed, the line under the preview says which setting is the likely cause.
  It updates whenever you move the frame or change a face setting; the FaceFusion helper keeps its models
  loaded while Faces is on, so each update takes well under a second.
- **Mask** (Target card): shades the face mask FaceFusion would blend the new face through, with your Face
  masker settings (types, padding, blur, areas, regions, occluder and parser models), on the faces that will be
  processed. The outline marks where the mask is half strength. It redraws as you change any mask setting.
- **Option labels**: in Studio a setting drops its group words (*Gender*, not *Face selector gender*), so most
  names fit on one line. In Favorites, where groups mix, those words show in small capitals underneath.
- **Gender**: Auto, Male and Female are a row of icon buttons instead of a drop-down.
- **Mask settings follow Types**: Areas, Regions and the occluder and parser models gray out while their type
  (area, region, occlusion) is unticked in Face masker Types, since FaceFusion ignores them then. Blur and
  Padding stay on, most processors use them whatever the types.
- **Face mapping**: a different source for each person in the target. Find people looks through the target
  (48 frames of a video, inside the trim) and lists each person with a picture, a name you can type and a
  source picker (a source, the selected sources, or Leave unchanged). The face boxes on the preview show the
  name and source, and a right click on a box renames it or picks its source. Start and Preview Frame then
  run FaceFusion once per mapped person on the target, in reference mode with that person's best frame, plus a
  base run that changes no face, and `compose_passes.py` adds the changed faces of all runs onto the base and
  encodes it once with your output settings. Because every run works on the original target, two people can
  even swap faces with each other. Frame processors (frame enhancer, colorizer, background remover) run once
  at the end. When everyone gets the same source it is a single normal run. Not for GIF targets yet.
- **Models**: pre-download the lite or full model set (`force-download`), see what's installed with sizes, and
  delete selected models (Ctrl/Shift for several, or the Delete key) to free disk space. FaceFusion
  downloads a deleted model again the next time it is used.
- **Benchmark**: shows the GPU, CPU, memory and ONNX Runtime providers, and runs FaceFusion's own
  `benchmark` command on its example videos (240p to 2160p) with the processors, provider, mode, cycle
  count and thread count you pick. Progress shows the run, resolution, cycle and stage. Results are kept
  in a table so you can compare runs (times are in seconds). **Export results** saves the selected rows,
  or the latest run, as a JPG with your hardware, ready to post, and copies it to the clipboard. The first
  run downloads all example videos into the FaceFusion folder.
- **Console**: the full FaceFusion log and the exact command line (copyable).
- **Setup**: point the app at an existing v4 checkout and Python, or install v4 from scratch. The installer
  clones the branch, creates a Python 3.13 conda or uv environment, optionally adds the CUDA runtime and cuDNN, and runs
  `install.py` with CUDA, DirectML, OpenVINO or CPU. Execution provider, threads, memory and download
  options live here too. *Updates* shows the Windows version and the installed FaceFusion version (branch,
  commit and date); **Check for Updates** looks up the same branch and the latest release on GitHub and
  says whether there are new commits, and **Open GitHub** opens the repository's page in the browser.
- Fluent design with Mica, light and dark themes that follow Windows, and settings remembered between runs.

## Install

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

This creates `.venv` for the UI (PySide6 + PySide6-Fluent-Widgets + OpenCV) and adds desktop and Start menu
shortcuts. Then start **FaceFusion Studio** and open **Setup**:

- **Already have v4?** Pick its folder and the `python.exe` of its environment, or press **Detect**, then press **Save and load options**.
- **No v4 yet?** Use **Install FaceFusion v4**. Note that v4 requires **Python 3.13+**, so a
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
| `ffstudio/pages/*` | Studio, Batch, Models, Benchmark, Console, Setup |

App data (settings, cached schema, preview frames) lives in `%APPDATA%\FaceFusionStudio`. The preview
folder only ever holds the latest frame and result (overwritten each time) and is deleted when the app
closes. After **Stop** or a failed run, FaceFusion's temp folder for this machine is cleared too.
Set `FFSTUDIO_DATA_DIR` to use a different folder.

## License

Released into the public domain under [The Unlicense](UNLICENSE). FaceFusion has its own license.
