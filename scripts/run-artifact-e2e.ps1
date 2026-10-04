param(
  [ValidateSet("focused", "full")]
  [string]$Scope = "full",
  [string]$ComposeProject = "mapreport-t15"
)

if (-not $env:E2E_SUPERADMIN_PASSWORD) {
  throw "E2E_SUPERADMIN_PASSWORD must be set for artifact browser acceptance."
}

$root = Split-Path -Parent $PSScriptRoot
$composeArgs = @(
  "compose",
  "-p",
  $ComposeProject,
  "--env-file",
  ".env",
  "-f",
  "infra/compose.yaml"
)
$environmentArgs = @("-e", "E2E_SUPERADMIN_PASSWORD")
if ($env:E2E_SUPERADMIN_USERNAME) {
  $environmentArgs += @("-e", "E2E_SUPERADMIN_USERNAME")
}

Push-Location $root
try {
  & docker @composeArgs run --rm @environmentArgs api `
    python -m tests.e2e_fixture
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }

  if ($Scope -eq "focused") {
    & docker @composeArgs run --rm @environmentArgs frontend `
      npm run test:e2e -- artifact-center.spec.ts loss-map.spec.ts `
      --reporter=line
  }
  else {
    & docker @composeArgs run --rm @environmentArgs frontend `
      npm run test:e2e -- --reporter=line
  }
  exit $LASTEXITCODE
}
finally {
  Pop-Location
}
