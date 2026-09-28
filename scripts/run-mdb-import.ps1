param(
  [Parameter(Mandatory = $true)][string]$AssetKey,
  [Parameter(Mandatory = $true)][string]$File,
  [Parameter(Mandatory = $true)][string]$Version,
  [Parameter(Mandatory = $true)][string]$SourceUri,
  [Parameter(Mandatory = $true)][string]$Actor
)

$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root ".env"
$values = @{}
Get-Content -LiteralPath $envFile | ForEach-Object {
  if ($_ -match '^\s*([^#=]+)=(.*)$') {
    $values[$matches[1].Trim()] = $matches[2].Trim()
  }
}
$hostPort = if ($values.ContainsKey("POSTGRES_HOST_PORT")) {
  $values["POSTGRES_HOST_PORT"]
} else {
  "5432"
}
$databaseUrl = $values["DATABASE_URL"] -replace "@postgres:5432", "@127.0.0.1:$hostPort"
$env:DATABASE_URL = $databaseUrl
$storageRoot = if ($values.ContainsKey("DATA_ASSET_STORAGE_HOST_DIR")) {
  [IO.Path]::GetFullPath(
    (Join-Path (Split-Path -Parent $envFile) $values["DATA_ASSET_STORAGE_HOST_DIR"])
  )
} else {
  Join-Path $root "data\data-assets"
}

Push-Location (Join-Path $root "backend")
try {
  python -m app.data_assets.mdb_cli import `
    --asset-key $AssetKey `
    --file $File `
    --version $Version `
    --source-uri $SourceUri `
    --actor $Actor `
    --storage-root $storageRoot
} finally {
  Pop-Location
}
