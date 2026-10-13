param([Parameter(Mandatory = $true)][string]$InstallerPath)
$ErrorActionPreference = 'Stop'
$path = (Resolve-Path -LiteralPath $InstallerPath).Path
$hash = Get-FileHash -LiteralPath $path -Algorithm SHA256
'{0}  {1}' -f $hash.Hash.ToLowerInvariant(), (Split-Path $path -Leaf) | Set-Content -LiteralPath "$path.sha256" -Encoding ascii
