# Creates the UI environment (.venv) and a Start menu + desktop shortcut.
# FaceFusion v4 itself lives in its own environment; install it from the app's Setup page.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (Get-Command uv -ErrorAction SilentlyContinue) {
	uv venv --python 3.12 .venv
	uv pip install --python .venv\Scripts\python.exe -r requirements.txt
} else {
	python -m venv .venv
	.venv\Scripts\python.exe -m pip install --upgrade pip
	.venv\Scripts\python.exe -m pip install -r requirements.txt
}

$shell = New-Object -ComObject WScript.Shell
$targets = @(
	[Environment]::GetFolderPath('Desktop'),
	(Join-Path ([Environment]::GetFolderPath('Programs')) '')
)
foreach ($folder in $targets) {
	# the app was called FaceFusion Studio before v0.30
	Remove-Item (Join-Path $folder 'FaceFusion Studio.lnk') -ErrorAction SilentlyContinue
	$shortcut = $shell.CreateShortcut((Join-Path $folder 'StudioFF.lnk'))
	$shortcut.TargetPath = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
	$shortcut.Arguments = '-m ffstudio'
	$shortcut.WorkingDirectory = $PSScriptRoot
	$shortcut.IconLocation = Join-Path $PSScriptRoot 'ffstudio\assets\icon.ico'
	$shortcut.Save()
}
Write-Host 'Done. Start "StudioFF" from the desktop or Start menu, or run StudioFF.bat'
