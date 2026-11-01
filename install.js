// StudioFF's environment first, then its own FaceFusion v4 (python -m ffstudio.pinokio install picks CUDA, DirectML or CPU for this PC)
module.exports = {
  requires: {
    bundle: "ai"
  },
  run: [{
    method: "shell.run",
    params: {
      venv: "env",
      message: [
        "uv pip install -r requirements.txt"
      ]
    }
  }, {
    method: "shell.run",
    params: {
      venv: "env",
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
