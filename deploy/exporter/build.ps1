param([Parameter(Mandatory = $true)][string]$NodeHome, [Parameter(Mandatory = $true)][string]$PythonHome)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$node = (Resolve-Path -LiteralPath $NodeHome).Path
$python = (Resolve-Path -LiteralPath $PythonHome).Path
if (-not (Test-Path -LiteralPath (Join-Path $node 'node.exe'))) { throw 'NodeHome must contain node.exe' }
if (-not (Test-Path -LiteralPath (Join-Path $node 'npm.cmd'))) { throw 'NodeHome must contain npm.cmd' }
if (-not (Test-Path -LiteralPath (Join-Path $python 'python.exe'))) { throw 'PythonHome must contain python.exe' }
$nodeVersion = & (Join-Path $node 'node.exe') --version
if ($nodeVersion -ne 'v26.4.0') { throw "Expected Node v26.4.0, got $nodeVersion" }
$npmVersion = & (Join-Path $node 'npm.cmd') --version
if ($npmVersion -ne '11.17.0') { throw "Expected npm 11.17.0, got $npmVersion" }
$pythonVersion = & (Join-Path $python 'python.exe') -c 'import platform; print(platform.python_version())'
if (-not $pythonVersion.StartsWith('3.12.')) { throw "Expected Python 3.12, got $pythonVersion" }
& (Join-Path $python 'python.exe') -m pip --version
if ($LASTEXITCODE -ne 0) { throw 'PythonHome must contain pip' }
foreach ($ui in @('exporter', 'importer')) {
    Push-Location (Join-Path $repo "frontend\$ui")
    try { npm ci; if ($LASTEXITCODE -ne 0) { throw "npm ci failed for $ui" }; npm test; if ($LASTEXITCODE -ne 0) { throw "tests failed for $ui" }; npm run build; if ($LASTEXITCODE -ne 0) { throw "build failed for $ui" } }
    finally { Pop-Location }
}
$stage = Join-Path $repo '.build\node-release'
$resolvedStage = [System.IO.Path]::GetFullPath($stage)
$expectedPrefix = [System.IO.Path]::GetFullPath((Join-Path $repo '.build')) + [System.IO.Path]::DirectorySeparatorChar
if (-not $resolvedStage.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid staging path' }
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Path (Join-Path $stage 'node_modules') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $node 'node.exe') -Destination $stage
Copy-Item -LiteralPath (Join-Path $node 'npm.cmd') -Destination $stage
Copy-Item -LiteralPath (Join-Path $node 'node_modules\npm') -Destination (Join-Path $stage 'node_modules') -Recurse
$env:PACKFERRY_NODE_HOME = $stage
$env:PACKFERRY_PYTHON_DIR = $python
Push-Location $repo
try {
    uv run --extra desktop --extra build pyinstaller --noconfirm --clean deploy/exporter/packferry.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
    $release = Join-Path $repo 'dist\packferry'
    $inputs = @(
        (Join-Path $stage 'node.exe'),
        (Join-Path $stage 'npm.cmd'),
        (Join-Path $stage 'node_modules\npm\package.json'),
        (Join-Path $python 'python.exe'),
        (Join-Path $repo 'apps\exporter\npm_helper.cjs'),
        (Join-Path $repo 'frontend\exporter\dist\index.html'),
        (Join-Path $repo 'frontend\importer\dist\index.html')
    )
    $inputs += (Get-Command uv).Source
    $manifest = $inputs | ForEach-Object { $hash = Get-FileHash -LiteralPath $_ -Algorithm SHA256; '{0}  {1}' -f $hash.Hash.ToLowerInvariant(), $hash.Path }
    Set-Content -LiteralPath (Join-Path $release 'runtime-sources.sha256') -Value $manifest -Encoding utf8
}
finally { Pop-Location }
