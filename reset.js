// the settings in the data folder stay, Install puts the rest back
module.exports = {
  run: [{
    method: "fs.rm",
    params: {
      path: "env"
    }
  }, {
    method: "fs.rm",
    params: {
      path: "facefusion"
    }
  }]
}
