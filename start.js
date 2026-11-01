// StudioFF opens in its own window, this script runs until that window closes
module.exports = {
  requires: {
    bundle: "ai"
  },
  run: [{
    method: "shell.run",
    params: {
      venv: "env",
      venv_python: "3.12",
      env: {
        FFSTUDIO_PINOKIO: "1"
      },
      message: [
        "python -m ffstudio"
      ]
    }
  }]
}
