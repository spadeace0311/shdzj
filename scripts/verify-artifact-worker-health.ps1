param(
  [string]$ComposeProject = "mapreport-t15"
)

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

Push-Location $root
try {
  $configJson = & docker @composeArgs config --format json
  if ($LASTEXITCODE -ne 0) {
    throw "docker compose config failed"
  }
  $config = $configJson | ConvertFrom-Json

  $expected = @{
    "artifact-worker" = @("CMD", "python", "-m", "app.process_health", "worker")
    "artifact-dispatcher" = @(
      "CMD",
      "python",
      "-m",
      "app.process_health",
      "dispatcher"
    )
  }
  foreach ($serviceName in $expected.Keys) {
    $actual = @($config.services.$serviceName.healthcheck.test)
    if (($actual -join "`n") -ne ($expected[$serviceName] -join "`n")) {
      throw "unexpected healthcheck for ${serviceName}: $($actual -join ' ')"
    }
  }

  & docker @composeArgs run --rm --no-deps api `
    python -m app.process_health worker
  if ($LASTEXITCODE -eq 0) {
    throw "missing artifact worker process unexpectedly passed the health check"
  }

  Write-Output "artifact worker/dispatcher health checks verified"
}
finally {
  Pop-Location
}
