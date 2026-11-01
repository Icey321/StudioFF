// a newer StudioFF from GitHub. FaceFusion is updated from StudioFF's FaceFusion page, which knows the tested commit
module.exports = {
  run: [{
    method: "shell.run",
    params: {
      message: "git pull"
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
  }]
}
