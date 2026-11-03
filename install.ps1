$ErrorActionPreference = "Stop"

$RepoSlug = "NoobyGains/usage-pulse"
$RepoUrl = if ($env:USAGE_PULSE_REPO_URL) { $env:USAGE_PULSE_REPO_URL } elseif ($env:CLAUDE_PULSE_REPO_URL) { $env:CLAUDE_PULSE_REPO_URL } else { "https://github.com/$RepoSlug.git" }
$RawBaseUrl = "https://raw.githubusercontent.com/$RepoSlug/main"

# New installs go to ~/.usage-pulse. An install from before the rename (3.5.0)
# stays in ~/.claude-pulse, so re-running this updates it in place.
$DefaultDir = Join-Path $HOME ".usage-pulse"
if (-not (Test-Path $DefaultDir) -and (Test-Path (Join-Path $HOME ".claude-pulse"))) {
  $DefaultDir = Join-Path $HOME ".claude-pulse"
}
$InstallDir = if ($env:USAGE_PULSE_DIR) { $env:USAGE_PULSE_DIR } elseif ($env:CLAUDE_PULSE_DIR) { $env:CLAUDE_PULSE_DIR } else { $DefaultDir }
$ClaudeDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME ".claude" }
$CommandsDir = Join-Path $ClaudeDir "commands"

$InstallMethod = ""

function Write-Step {
  param([string]$Message)
  Write-Host $Message
}

function Throw-IfFailed {
  param([string]$Message)
  if ($LASTEXITCODE -ne 0) {
    throw $Message
  }
}

function Download-Files {
  New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
  Invoke-WebRequest -Uri "$RawBaseUrl/claude_status.py" -OutFile (Join-Path $InstallDir "claude_status.py")
  New-Item -ItemType Directory -Path (Join-Path $InstallDir "commands") -Force | Out-Null
  Invoke-WebRequest -Uri "$RawBaseUrl/commands/pulse.md" -OutFile (Join-Path (Join-Path $InstallDir "commands") "pulse.md")
  $script:InstallMethod = "raw"
}

if (Get-Command git -ErrorAction SilentlyContinue) {
  if (Test-Path (Join-Path $InstallDir ".git")) {
    $originUrl = (git -C $InstallDir remote get-url origin).Trim()
    Throw-IfFailed "Failed to read git origin from $InstallDir"
    if ($originUrl -notmatch "NoobyGains/(usage|claude)-pulse(\.git)?$") {
      throw "Existing git repo at '$InstallDir' has unexpected origin '$originUrl'"
    }

    Write-Step "Updating existing usage-pulse clone..."
    git -C $InstallDir pull --ff-only origin main | Out-Null
    Throw-IfFailed "Failed to update git clone"
    $InstallMethod = "git"
  }
  elseif (Test-Path (Join-Path $InstallDir "claude_status.py")) {
    Write-Step "Existing non-git install detected, refreshing files..."
    Download-Files
  }
  elseif (Test-Path $InstallDir) {
    throw "Directory already exists and is not a usage-pulse install: $InstallDir"
  }
  else {
    $parentDir = Split-Path -Parent $InstallDir
    if ($parentDir -and -not (Test-Path $parentDir)) {
      New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
    }

    Write-Step "Cloning usage-pulse..."
    git clone --depth 1 $RepoUrl $InstallDir | Out-Null
    Throw-IfFailed "Failed to clone repository"
    $InstallMethod = "git"
  }
}
else {
  Write-Step "git not found, downloading scripts directly..."
  Download-Files
}

$PulseCommandPath = Join-Path (Join-Path $InstallDir "commands") "pulse.md"
if (Test-Path $PulseCommandPath) {
  New-Item -ItemType Directory -Path $CommandsDir -Force | Out-Null
  Copy-Item -Path $PulseCommandPath -Destination (Join-Path $CommandsDir "pulse.md") -Force
}

$StatusScriptPath = Join-Path $InstallDir "claude_status.py"

# The wizard needs a real console on both ends. `irm | iex` runs in the
# caller's console, so it qualifies; redirected or console-less hosts (CI,
# ISE) get the defaults. USAGE_PULSE_NONINTERACTIVE=1 always takes them.
$Interactive = $false
if (-not ($env:USAGE_PULSE_NONINTERACTIVE -or $env:CLAUDE_PULSE_NONINTERACTIVE)) {
  try {
    $Interactive = (-not [Console]::IsInputRedirected) -and (-not [Console]::IsOutputRedirected)
  }
  catch {
    $Interactive = $false
  }
}
$SetupArgs = if ($Interactive) { @("--setup") } else { @("--setup", "--non-interactive") }

# A command that exists is not a command that works. With the default App
# execution aliases switched on, `python` resolves to a Microsoft Store stub in
# WindowsApps that exists but only complains, and a python.org install without
# "Add python.exe to PATH" has no `python` at all. So every candidate has to run
# a real Python 3.8+ before it is picked.
function Test-PythonCommand {
  param([string]$Exe, [string[]]$PreArgs = @())
  $previous = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & $Exe @PreArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" *> $null
    return ($LASTEXITCODE -eq 0)
  }
  catch {
    return $false
  }
  finally {
    $ErrorActionPreference = $previous
  }
}

# The py launcher goes first: it is installed by python.org and the Python
# install manager, it skips the Store stub, and it keeps pointing at a real
# Python after an upgrade, which a versioned python.exe path does not.
function Find-Python {
  if ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-PythonCommand -Exe "py" -PreArgs @("-3"))) {
    return @{ Exe = "py"; PreArgs = @("-3") }
  }
  foreach ($name in @("python", "python3")) {
    $found = @(Get-Command $name -All -ErrorAction SilentlyContinue | Where-Object { $_.CommandType -eq "Application" })
    foreach ($candidate in $found) {
      if (Test-PythonCommand -Exe $candidate.Source) {
        return @{ Exe = $candidate.Source; PreArgs = @() }
      }
    }
  }
  return $null
}

$Python = Find-Python
if (-not $Python) {
  throw "Python 3.8 or newer is required. Install Python (python.org or the Microsoft Store), then run this installer again."
}
$PythonExe = $Python.Exe
$PythonPreArgs = $Python.PreArgs
& $PythonExe @PythonPreArgs $StatusScriptPath @SetupArgs
Throw-IfFailed "usage-pulse setup failed"

Write-Host ""
Write-Host "usage-pulse installed in: $InstallDir"
Write-Host "Restart Claude Code. Change anything later with /pulse (or /pulse setup)."
if ($InstallMethod -eq "raw") {
  Write-Host "Note: installed without git. /pulse update expects a git clone."
}
