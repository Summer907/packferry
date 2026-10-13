$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Push-Location $repo
try {
    uv run --extra build pyinstaller --noconfirm --clean deploy/importer-client/packferry-import.spec
    if ($LASTEXITCODE -ne 0) { throw 'Importer client build failed' }
}
finally { Pop-Location }
