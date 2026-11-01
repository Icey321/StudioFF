# Creates the UI environment (.venv) and a Start menu + desktop shortcut.
# FaceFusion v4 itself lives in its own environment; install it from the app's FaceFusion page.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Test-Python {
	# the Microsoft Store placeholder python.exe only opens the Store, it can't build a venv
	$python = Get-Command python -ErrorAction SilentlyContinue
	if (-not $python -or $python.Source -like '*\WindowsApps\*') {
		return $false
	}
	try {
		return (& python -c 'import sys; print(sys.version_info >= (3, 10))' 2>$null) -eq 'True'
	} catch {
		return $false
	}
}

function Update-Path {
	# winget adds uv to PATH for new windows only, this window reads it again
	$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
	$links = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'
	if (Test-Path $links) {
		$env:Path += ';' + $links
	}
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue) -and -not (Test-Python)) {
	Write-Host ''
	Write-Host 'StudioFF needs Python to run, and neither Python 3.10+ nor uv was found on this PC.'
	Write-Host ''
	Write-Host 'This script can install uv for you:'
	Write-Host '  - What:  uv by Astral (https://docs.astral.sh/uv/), a small free tool that sets up Python.'
	Write-Host '  - How:   Windows Package Manager (winget), package id astral-sh.uv.'
	Write-Host '  - Then:  uv downloads Python 3.12 into its own folder for StudioFF only.'
	Write-Host '           Your system Python, if you add one later, is not changed.'
	Write-Host '  - Size:  about 20 MB for uv, about 30 MB for Python.'
	Write-Host 'Nothing else is installed. Windows may ask for your permission.'
	Write-Host ''
	if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
		Write-Host 'winget is not available on this PC, so uv cannot be installed from here.'
		Write-Host 'Install uv from https://docs.astral.sh/uv/ or Python 3.12 from https://www.python.org, then run install.ps1 again.'
		exit 1
	}
	$answer = Read-Host 'Install uv now? (Y/N)'
	if ($answer -notmatch '^(y|yes)$') {
		Write-Host 'Nothing was installed. Install uv or Python 3.12 yourself, then run install.ps1 again.'
		exit 1
	}
	winget install --id astral-sh.uv --exact --silent --accept-package-agreements --accept-source-agreements
	Update-Path
	if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
		Write-Host 'uv was installed but this window cannot find it yet. Close this window, open a new terminal here and run install.ps1 again.'
		exit 1
	}
}

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
