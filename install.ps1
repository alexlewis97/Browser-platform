#Requires -Version 5.1
<#
.SYNOPSIS
  One-click bootstrap for Browser Platform on Windows (no admin).

.DESCRIPTION
  Installs or uses `uv` to get Python 3.11+ and project dependencies into
  a local .venv, installs Node LTS per-user (no admin) if missing, runs
  `npm install` for official Pomerado in node_host, installs Playwright
  Chromium, and creates a Desktop shortcut named "Browser Platform".

  Re-run after unzipping a newer browser-platform-windows.zip over this
  folder (or just double-click start.bat) to upgrade packages. Workflows
  stay in %LOCALAPPDATA%\BrowserPlatform\data. Pass -Update to download
  the latest zip first (requires -UpdateUrl or update-url.txt).

.PARAMETER Update
  Download the latest zip and extract it over this folder, then reinstall.

.PARAMETER UpdateUrl
  HTTPS URL of browser-platform-windows.zip. Overrides update-url.txt.
#>
[CmdletBinding()]
param(
    [switch]$Update,
    [string]$UpdateUrl = $env:BROWSER_PLATFORM_UPDATE_URL
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
# PS 5.1: prefer TLS 1.2 for installs without admin.
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Get-InstallRoot {
    if ($PSScriptRoot) { return (Resolve-Path -LiteralPath $PSScriptRoot).Path }
    return (Resolve-Path -LiteralPath (Split-Path -Parent $MyInvocation.MyCommand.Path)).Path
}

function Add-UserPath([string]$Directory) {
    if (-not $Directory -or -not (Test-Path -LiteralPath $Directory)) { return }
    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $current) { $current = "" }
    $parts = $current -split ";" | Where-Object { $_ -and $_.Trim() }
    if ($parts -contains $Directory) {
        if (-not ($env:Path -split ";" | Where-Object { $_ -eq $Directory })) {
            $env:Path = "$Directory;$env:Path"
        }
        return
    }
    [Environment]::SetEnvironmentVariable("Path", (($parts + $Directory) -join ";"), "User")
    $env:Path = "$Directory;$env:Path"
}

function Get-UpdateUrl([string]$Root, [string]$Override) {
    if ($Override) { return $Override.Trim() }
    $file = Join-Path $Root "update-url.txt"
    if (Test-Path -LiteralPath $file) {
        $line = Get-Content -LiteralPath $file | Where-Object { $_ -and ($_ -notmatch "^\s*#") } | Select-Object -First 1
        if ($line) { return $line.Trim() }
    }
    return ""
}

function Update-FromZip([string]$Root, [string]$Url) {
    if (-not $Url) {
        throw "No update URL configured. Pass -UpdateUrl or set update-url.txt (refusing to pretend the upgrade succeeded)."
    }
    if ($Url -notmatch "^https://") {
        throw "Update URL must be HTTPS."
    }
    Write-Step "Downloading latest zip"
    $tmp = Join-Path $env:TEMP ("browser-platform-update-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    $zip = Join-Path $tmp "browser-platform-windows.zip"
    try {
        Invoke-WebRequest -Uri $Url -OutFile $zip -UseBasicParsing
        # Prefer a sibling .sha256 file published next to the zip.
        $shaUrl = if ($Url -match "\.sha256$") { $Url } else { "$Url.sha256" }
        $expected = $null
        try {
            $shaBody = (Invoke-WebRequest -Uri $shaUrl -UseBasicParsing).Content
            if ($shaBody -match "([A-Fa-f0-9]{64})") { $expected = $Matches[1].ToLowerInvariant() }
        } catch {
            throw "Update refused: could not download $shaUrl (publish a sha256 next to the zip)."
        }
        if (-not $expected) { throw "Update refused: sha256 file did not contain a 64-hex digest." }
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
        if ($actual -ne $expected) {
            throw "Update refused: zip sha256 mismatch (expected $expected, got $actual)."
        }
        Write-Host "  sha256 ok ($actual)"
        $extract = Join-Path $tmp "extracted"
        Expand-Archive -LiteralPath $zip -DestinationPath $extract -Force
        # Prefer the directory that actually contains install.ps1.
        $withInstall = Get-ChildItem -LiteralPath $extract -Recurse -Filter "install.ps1" -File -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($withInstall) {
            $sourceCandidate = Split-Path -Parent $withInstall.FullName
        } else {
            $sourceCandidate = $null
        }
        $source = if ($sourceCandidate) { $sourceCandidate } else { $extract }
        Write-Step "Applying update (preserving .venv and local data)"
        $exclude = @(".venv", "data", ".env", "node_modules", ".install-stamp")
        Get-ChildItem -LiteralPath $source -Force | ForEach-Object {
            if ($exclude -contains $_.Name) { return }
            $dest = Join-Path $Root $_.Name
            if ($_.PSIsContainer) {
                if (Test-Path -LiteralPath $dest) {
                    # Skip copying .venv-like trees if they slipped into the zip
                    Copy-Item -LiteralPath $_.FullName -Destination $dest -Recurse -Force
                } else {
                    Copy-Item -LiteralPath $_.FullName -Destination $dest -Recurse -Force
                }
            } else {
                Copy-Item -LiteralPath $_.FullName -Destination $dest -Force
            }
        }
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Install-Uv {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) { return $uv.Source }
    $candidates = @(
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
        (Join-Path $env:USERPROFILE ".cargo\bin\uv.exe"),
        (Join-Path $env:LOCALAPPDATA "uv\uv.exe")
    )
    foreach ($path in $candidates) {
        if (Test-Path -LiteralPath $path) {
            Add-UserPath (Split-Path -Parent $path)
            return $path
        }
    }
    Write-Step "Installing uv (Python package manager, user-level, no admin)"
    $installer = Join-Path $env:TEMP "uv-install.ps1"
    Invoke-WebRequest -Uri "https://astral.sh/uv/install.ps1" -OutFile $installer -UseBasicParsing
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $installer
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
    foreach ($path in $candidates) {
        if (Test-Path -LiteralPath $path) {
            Add-UserPath (Split-Path -Parent $path)
            return $path
        }
    }
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) { return $uv.Source }
    throw "uv installed but was not found on PATH. Open a new PowerShell window and re-run install.ps1."
}


function Get-NodeExe {
    $cmd = Get-Command node -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Path -LiteralPath $cmd.Source)) { return $cmd.Source }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "BrowserPlatform\runtime\node\node.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\node\node.exe"),
        (Join-Path ${env:ProgramFiles} "nodejs\node.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "nodejs\node.exe")
    )
    foreach ($path in $candidates) {
        if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    $homeFile = Join-Path $env:LOCALAPPDATA "BrowserPlatform\runtime\node-home.txt"
    if (Test-Path -LiteralPath $homeFile) {
        $dir = (Get-Content -LiteralPath $homeFile -TotalCount 1).Trim()
        $exe = Join-Path $dir "node.exe"
        if (Test-Path -LiteralPath $exe) { return $exe }
    }
    return $null
}

function Save-NodeHome([string]$NodeExe) {
    $dir = Split-Path -Parent $NodeExe
    $runtime = Join-Path $env:LOCALAPPDATA "BrowserPlatform\runtime"
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $runtime "node-home.txt") -Value $dir -Encoding ASCII
    Add-UserPath $dir
}

function Install-NodeLts {
    $existing = Get-NodeExe
    if ($existing) {
        Save-NodeHome $existing
        return $existing
    }
    Write-Step "Installing Node.js LTS (per-user, no admin) for Pomerado"

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        try {
            & $winget.Source install --id OpenJS.NodeJS.LTS -e --scope user --accept-package-agreements --accept-source-agreements --disable-interactivity
        } catch {
            Write-Host "winget Node install failed; trying a portable zip." -ForegroundColor Yellow
        }
        # Refresh PATH for this session from the user PATH.
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        if ($userPath) { $env:Path = "$userPath;$env:Path" }
        $existing = Get-NodeExe
        if ($existing) {
            Save-NodeHome $existing
            return $existing
        }
    }

    $dest = Join-Path $env:LOCALAPPDATA "BrowserPlatform\runtime\node"
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "x64" }
    $index = Invoke-RestMethod -Uri "https://nodejs.org/dist/index.json" -UseBasicParsing
    $release = $index | Where-Object { $_.lts } | Select-Object -First 1
    if (-not $release) { throw "Could not resolve the current Node.js LTS version." }
    $ver = $release.version
    $name = "node-$ver-win-$arch.zip"
    $url = "https://nodejs.org/dist/$ver/$name"
    $tmp = Join-Path $env:TEMP ("node-lts-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    $zip = Join-Path $tmp $name
    try {
        Write-Host "Downloading $name"
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        $extract = Join-Path $tmp "extracted"
        Expand-Archive -LiteralPath $zip -DestinationPath $extract -Force
        $inner = Get-ChildItem -LiteralPath $extract -Directory | Select-Object -First 1
        $source = if ($inner -and (Test-Path -LiteralPath (Join-Path $inner.FullName "node.exe"))) { $inner.FullName } else { $extract }
        Get-ChildItem -LiteralPath $source -Force | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $dest $_.Name) -Recurse -Force
        }
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
    $exe = Join-Path $dest "node.exe"
    if (-not (Test-Path -LiteralPath $exe)) {
        throw "Portable Node.js was extracted but node.exe was not found in $dest"
    }
    Save-NodeHome $exe
    return $exe
}

function Install-PomeradoHost([string]$Root, [string]$NodeExe) {
    $hostPkg = Join-Path $Root "node_host\package.json"
    if (-not (Test-Path -LiteralPath $hostPkg)) {
        Write-Host "node_host/package.json is missing; skipping Pomerado." -ForegroundColor Yellow
        return
    }
    $nodeDir = Split-Path -Parent $NodeExe
    $npmCmd = Join-Path $nodeDir "npm.cmd"
    if (-not (Test-Path -LiteralPath $npmCmd)) { $npmCmd = "npm" }
    Write-Step "Installing official Pomerado (npm install in node_host)"
    Push-Location (Join-Path $Root "node_host")
    try {
        $env:PATH = "$nodeDir;$env:PATH"
        if (Test-Path -LiteralPath (Join-Path (Get-Location) "package-lock.json")) {
            & $npmCmd ci --omit=dev
            if ($LASTEXITCODE -ne 0) { & $npmCmd install --omit=dev }
        } else {
            & $npmCmd install --omit=dev
        }
        if ($LASTEXITCODE -ne 0) { throw "npm install in node_host failed." }
        # Optional browser download if Chrome is not already on this machine.
        $chrome = @(
            "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
            "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
            "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
        ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if (-not $chrome) {
            Write-Step "Installing Playwright Chromium for Pomerado (no system Chrome found)"
            & $npmCmd exec -- playwright install chromium
        }
    } finally {
        Pop-Location
    }
}

function New-DesktopShortcut([string]$Root) {
    Write-Step "Creating Desktop shortcut 'Browser Platform'"
    $desktop = [Environment]::GetFolderPath("Desktop")
    if (-not $desktop) { $desktop = Join-Path $env:USERPROFILE "Desktop" }
    $lnkPath = Join-Path $desktop "Browser Platform.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($lnkPath)
    $shortcut.TargetPath = Join-Path $Root "start.bat"
    $shortcut.WorkingDirectory = $Root
    $shortcut.WindowStyle = 1
    $shortcut.Description = "Start Browser Platform at http://localhost:8745"
    $icon = Join-Path $env:LOCALAPPDATA "BrowserPlatform\chrome-profile\Icon"
    # Prefer a Chrome icon if the installed browser is present.
    $chromeIcon = @(
        "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($chromeIcon) { $shortcut.IconLocation = "$chromeIcon,0" }
    $shortcut.Save()
    Write-Host "Shortcut: $lnkPath"
}

$Root = Get-InstallRoot
Set-Location -LiteralPath $Root

if ($Update) {
    $url = Get-UpdateUrl -Root $Root -Override $UpdateUrl
    Update-FromZip -Root $Root -Url $url
}

$uv = Install-Uv
Write-Step "Creating Python 3.11+ virtualenv with uv"
& $uv python install 3.11
if (-not (Test-Path -LiteralPath (Join-Path $Root ".venv\Scripts\python.exe"))) {
    & $uv venv --python 3.11 .venv
}

$python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "venv was created but .venv\Scripts\python.exe is missing."
}

Write-Step "Installing Python package (browser-use + workflow-use included)"
try {
    # Quote the extras so PowerShell does not treat [browseruse] as a wildcard.
    & $uv pip install --python $python -e ".[browseruse,workflowuse]"
} catch {
    Write-Host "Optional extras failed; installing core package only." -ForegroundColor Yellow
    & $uv pip install --python $python -e "."
}

Write-Step "Installing Playwright Chromium (used if Chrome is not installed)"
& $python -m playwright install chromium

$nodeExe = Install-NodeLts
Install-PomeradoHost -Root $Root -NodeExe $nodeExe

$stampSrc = Join-Path $Root "release-id.txt"
$stampDst = Join-Path $Root ".install-stamp"
if (Test-Path -LiteralPath $stampSrc) {
    Copy-Item -LiteralPath $stampSrc -Destination $stampDst -Force
} else {
    Set-Content -LiteralPath $stampDst -Value ((Get-Date).ToString("o"))
}

New-DesktopShortcut -Root $Root

Write-Host ""
Write-Host "Done. Double-click the 'Browser Platform' desktop shortcut (or start.bat)." -ForegroundColor Green
Write-Host "The app opens at http://localhost:8745. No tunnel is started."
Write-Host "In Settings, paste your OpenAI key for Pomerado minting (saved only on this"
Write-Host "computer under %LOCALAPPDATA%\BrowserPlatform\secrets) and your OpenRouter"
Write-Host "key for from-task / repair (stays in this browser's localStorage)."
Write-Host "Workflows are kept in %LOCALAPPDATA%\BrowserPlatform\data across upgrades."
