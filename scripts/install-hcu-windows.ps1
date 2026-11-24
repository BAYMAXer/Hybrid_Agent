#Requires -Version 7.0
param(
    [Parameter(Mandatory)][string]$SourceDirectory,
    [string]$Destination = (Join-Path $env:LOCALAPPDATA 'HybridAgent/hcu/697d3fb')
)
$ErrorActionPreference = 'Stop'
$revision = '697d3fbda04cb322915866bab1aad79c726a61e3'
$source = (Resolve-Path -LiteralPath $SourceDirectory).Path
$destinationPath = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $destinationPath) {
    $marker = Join-Path $destinationPath 'REVISION'
    if ((Test-Path -LiteralPath $marker) -and (Get-Content -LiteralPath $marker -Raw).Trim() -eq $revision) {
        foreach ($name in @('computer-use.exe', 'computer-use-windows-provider.exe', 'hcu_sdk/__init__.py', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'licenses/windows-provider')) {
            if (-not (Test-Path -LiteralPath (Join-Path $destinationPath $name))) { throw "Incomplete installation at $destinationPath" }
        }
        Write-Output "HCU already installed at $destinationPath"
        return
    }
    throw "Destination exists. Choose a new directory: $destinationPath"
}
foreach ($tool in @('git', 'go', 'dotnet')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool is required on PATH" }
}
$parent = Split-Path -Parent $destinationPath
[void][IO.Directory]::CreateDirectory($parent)
$nonce = [guid]::NewGuid().ToString('N')
$stage = Join-Path $parent ('.hcu-stage-' + $nonce)
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/')
$temporary = Join-Path $tempRoot ('hybrid-hcu-' + $nonce)
[void][IO.Directory]::CreateDirectory($temporary)
$archive = Join-Path $temporary 'source.zip'
$checkout = Join-Path $temporary 'source'
$output = Join-Path $temporary 'build'
try {
    # Build only the pinned Git objects, even if the user is editing their checkout.
    git -C $source archive --format=zip "--output=$archive" $revision -- computer-use sdk/python/hcu_sdk VERSION LICENSE THIRD_PARTY_NOTICES.md licenses
    if ($LASTEXITCODE -ne 0) { throw "Could not export the pinned HCU revision" }
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $checkout)
    & (Join-Path $checkout 'computer-use/native/windows/build.ps1') -OutputDirectory $output
    if ($LASTEXITCODE -ne 0) { throw "HCU build failed" }
    [void][IO.Directory]::CreateDirectory($stage)
    foreach ($name in @('computer-use.exe', 'computer-use-windows-provider.exe', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'licenses')) {
        Copy-Item -LiteralPath (Join-Path $output $name) -Destination $stage -Recurse
    }
    Copy-Item -LiteralPath (Join-Path $checkout 'sdk/python/hcu_sdk') -Destination $stage -Recurse
    Set-Content -LiteralPath (Join-Path $stage 'REVISION') -Value $revision -Encoding utf8NoBOM
    [IO.Directory]::Move($stage, $destinationPath)
    Write-Output "Installed HCU $revision at $destinationPath"
} finally {
    foreach ($owned in @(@($tempRoot, ('hybrid-hcu-' + $nonce)), @($parent, ('.hcu-stage-' + $nonce)))) {
        # Resolve and bound each unique staging directory before recursive cleanup.
        $candidate = Join-Path $owned[0] $owned[1]
        if (Test-Path -LiteralPath $candidate) {
            $resolved = (Resolve-Path -LiteralPath $candidate).Path
            if ([IO.Path]::GetDirectoryName($resolved) -eq $owned[0] -and [IO.Path]::GetFileName($resolved) -eq $owned[1]) {
                Remove-Item -LiteralPath $resolved -Recurse -Force
            }
        }
    }
}
