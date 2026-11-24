[CmdletBinding()]
param(
    [string]$PythonPath,
    [ValidateRange(1, 65535)]
    [int]$Port = $(if ($env:AGENTS_VIZ_PORT) { [int]$env:AGENTS_VIZ_PORT } else { 8090 }),
    [switch]$OpenBrowser
)

$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $PSScriptRoot
$packageName = Split-Path -Leaf $packageRoot
$projectRoot = Split-Path -Parent $packageRoot
$studioUrl = "http://127.0.0.1:$Port"
$taskUrl = "$studioUrl/#run"

$waitForStudio = {
    param($Url, $Root, $LaunchBrowser, $Attempts)
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            $info = Invoke-RestMethod -Uri "$Url/runtime/info" -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop
        } catch {
            $info = $null
        }
        if ($info.service -eq 'yaero-studio' -and $info.project_root -eq $Root) {
            if ($LaunchBrowser) { Start-Process "$Url/#run" }
            return $true
        }
        if ($attempt -lt $Attempts) { Start-Sleep -Milliseconds 500 }
    }
    return $false
}

if (& $waitForStudio $studioUrl $packageRoot $OpenBrowser 1) {
    Write-Host "Studio is already running. Tasks and live trajectory: $taskUrl"
    return
}

if ($PythonPath) {
    $python = (Get-Item -LiteralPath $PythonPath -ErrorAction Stop).FullName
} else {
    $candidates = @(
        (Join-Path $projectRoot '.venv\Scripts\python.exe')
        (Join-Path $packageRoot '.venv\Scripts\python.exe')
        if ($env:VIRTUAL_ENV) { Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe' }
        if ($env:OSWORLD_RUNNER_PYTHON) { $env:OSWORLD_RUNNER_PYTHON }
        (Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
    )
    $python = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Select-Object -First 1
}
if (-not $python) {
    throw "Python 3.12 was not found. Follow the Windows setup in $packageRoot\README.md, or use -PythonPath with your configured interpreter."
}

Push-Location -LiteralPath $projectRoot
$browserJob = $null
try {
    & $python -c "import fastapi, uvicorn, httpx, requests, yaml, PIL, playwright, websockets"
    if ($LASTEXITCODE -ne 0) {
        throw "Studio dependencies are missing from $python. Install requirements\requirements-windows.txt there, or use -PythonPath with your configured interpreter."
    }
    $env:AGENTS_VIZ_PORT = [string]$Port
    Write-Host "Studio tasks and live trajectory: $taskUrl"
    Write-Host "Python: $python"
    if ($OpenBrowser) {
        # Open the page only after this workspace's server is responding.
        $browserJob = Start-Job -ScriptBlock $waitForStudio -ArgumentList $studioUrl, $packageRoot, $true, 60
    }
    & $python -m "$packageName.viz.server"
    if ($LASTEXITCODE -ne 0) { throw "Studio exited with code $LASTEXITCODE. See the server output above." }
} finally {
    if ($browserJob) { Remove-Job $browserJob -Force }
    Pop-Location
}
