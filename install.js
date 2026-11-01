// StudioFF's environment first, then its own FaceFusion v4 (python -m ffstudio.pinokio install picks CUDA, DirectML or CPU for this PC)
// venv_python makes Pinokio build env with uv on Python 3.12 (its own Python is 3.10, too old for the app's packages)
module.exports = {
  requires: {
    bundle: "ai"
  },
  run: [{
    method: "fs.rm",
    params: {
      path: "env"
    }
  }, {
    method: "shell.run",
    params: {
      venv: "env",
      venv_python: "3.12",
      message: [
        "uv pip install -r requirements.txt"
      ],
      on: [{
        event: "/No solution found/i",
        break: true
      }]
    }
  }, {
    method: "shell.run",
    params: {
      venv: "env",
      venv_python: "3.12",
      message: [
        "python -m ffstudio.pinokio install"
      ]
    }
  }, {
    method: "notify",
    params: {
      html: "StudioFF is installed. Press Start."
    }
  }]
}
