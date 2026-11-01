// the menu Pinokio shows for StudioFF. install.js puts StudioFF's own FaceFusion v4 in the facefusion folder
module.exports = {
  version: "3.7",
  title: "StudioFF",
  description: "A desktop app for the FaceFusion v4 face swapper: previews, face mapping and batches. https://github.com/Icey321/StudioFF",
  icon: "ffstudio/assets/icon.png",
  menu: async (kernel, info) => {
    let installed = info.exists("env") && info.exists("facefusion/.conda")
    let running = {
      install: info.running("install.js"),
      start: info.running("start.js"),
      update: info.running("update.js"),
      reset: info.running("reset.js")
    }
    if (running.install) {
      return [{ default: true, icon: "fa-solid fa-plug", text: "Installing", href: "install.js" }]
    }
    if (running.update) {
      return [{ default: true, icon: "fa-solid fa-terminal", text: "Updating", href: "update.js" }]
    }
    if (running.reset) {
      return [{ default: true, icon: "fa-solid fa-terminal", text: "Resetting", href: "reset.js" }]
    }
    if (!installed) {
      return [{ default: true, icon: "fa-solid fa-plug", text: "Install", href: "install.js" }]
    }
    if (running.start) {
      // StudioFF is a window of its own, the terminal shows what it prints
      return [{ default: true, icon: "fa-solid fa-terminal", text: "Terminal", href: "start.js" }]
    }
    return [{
      default: true,
      icon: "fa-solid fa-power-off",
      text: "Start",
      href: "start.js"
    }, {
      icon: "fa-solid fa-rotate",
      text: "Update",
      href: "update.js"
    }, {
      icon: "fa-solid fa-plug",
      text: "Install",
      href: "install.js"
    }, {
      icon: "fa-regular fa-circle-xmark",
      text: "Reset",
      href: "reset.js",
      confirm: "Remove StudioFF's Python environment and its FaceFusion (with its downloaded models)? Your StudioFF settings in the data folder stay."
    }]
  }
}
