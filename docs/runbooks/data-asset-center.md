# Data Asset Center Runbook

## Scope

The data asset center stores versioned vector, table, raster, and parameter data assets for a region. It supports import, validation, publication, retirement, rollback, snapshot inspection, update-overdue detection, and host-file backup.

Implemented in this branch:

- Asset catalog for region `shanghai`.
- GeoJSON, MDB, GeoTIFF, and YAML/JSON parameter-file import.
- Managed import jobs with queued, running, completed, rejected, and failed states.
- Candidate version lifecycle: `imported -> validated -> published -> retired`, with `retired -> published` rollback and terminal `rejected`.
- PostGIS vector records, JSONB table properties, PostGIS raster storage, audit logs, and content-addressed source files.
- Assessment-run asset snapshots that freeze the published version, checksum, and required/optional role for every run.

The loss formulas remain future work. Snapshotting a published asset is already implemented, but the later loss tasks will consume those snapshots.

## Required Configuration

- `DATA_ASSET_REGION_ID`: catalog region, default `shanghai`.
- `DATA_ASSET_REQUIRED_REGISTRY_PATH`: required/optional asset registry, default `/config/data_assets/shanghai-required-assets.yaml`.
- `DATA_ASSET_STORAGE_ROOT`: in-container raw-file root, default `/var/lib/data-assets`.
- `DATA_ASSET_STORAGE_HOST_DIR`: host bind mount backing that root, default `../data/data-assets`.
- `DATA_ASSET_MAX_UPLOAD_BYTES`: maximum source file size, default `1073741824`.
- `DATA_ASSET_MDB_DRIVER`: Windows Access ODBC driver, default `Microsoft Access Driver (*.mdb, *.accdb)`.
- `DATA_ASSET_WORKER_POLL_SECONDS`: idle poll interval for the import worker, default `1.0`.

The first catalog uses region `shanghai`, but all API and SQL queries include `region_id`. New province or district deployments must choose a distinct region and provide a matching required-asset registry.

The default host path is relative to `infra/compose.yaml`, so it resolves to `<repository-root>\data\data-assets` on Windows. In the Task 9 worktree that is `C:\Users\User\.codex\worktrees\assessment-orchestration\地震应急辅助决策系统\data\data-assets`. The repository-root `.gitignore` rule `/data/` excludes this default host storage directory.

## Starting The API And Import Worker

From the repository root:

```powershell
docker compose --env-file .env -f infra/compose.yaml up -d postgres api data-asset-worker
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic upgrade head
```

Confirm the migration head:

```powershell
docker compose --env-file .env -f infra/compose.yaml run --rm api alembic current
```

Expected: `0012_data_asset_center (head)`.

Authenticate before API calls:

```powershell
$form = @{
  username = "<username>"
  password = "<password>"
}
$tokenResponse = Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/auth/login" `
  -Method Post `
  -Body $form
$headers = @{ Authorization = "Bearer $($tokenResponse.access_token)" }
```

Roles:

- `data_maintainer`: import and validate.
- `data_publisher`: import and validate, plus publish, retire, and roll back.
- `superadmin`: all data asset operations.

The API route uses `data_maintainer` and `data_publisher` in its write-role tuple and `data_publisher` in its publish-role tuple, so publisher import access is intentional.

## Importing GeoJSON

GeoJSON is processed by `data-asset-worker` in the Linux container. The supported catalog keys are:

- `shanghai.admin.city`
- `shanghai.admin.county`
- `shanghai.admin.town`
- `shanghai.fault`

The source must be a GeoJSON `Feature` or `FeatureCollection`; geometry is normalized to the contract type and to `EPSG:4326`.

```powershell
$file = Get-Item "D:\path\to\boundary.geojson"
$form = @{
  version      = "2022.1"
  source_uri   = "https://example.gov.invalid/shanghai-admin-town-2022"
  license_name = "example-license"
  change_note  = "initial import"
  file         = $file
}
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/data-assets/shanghai.admin.town/import" `
  -Method Post `
  -Headers $headers `
  -Form $form
```

The response contains `job_id`, `version_id`, and the initial `queued` status. Poll the worker through the version or import-job query below; it performs normalization and validation.

## Importing The Shanghai MDB On Windows

MDB import runs on the Windows host because the Microsoft Access ODBC driver is not available in the Linux API container. The importer opens the file read-only (`ReadOnly=True`) and never updates the source file.

Use Python 3.12 and the 64-bit `Microsoft Access Driver (*.mdb, *.accdb)` driver. The script reads `.env`, connects to `127.0.0.1:5432`, and writes the content-addressed source copy under the host-side storage directory.

```powershell
.\scripts\run-mdb-import.ps1 `
  -AssetKey shanghai.population.town `
  -File "D:\地震应急辅助决策系统\基础数据\上海应急基础数据2022.mdb" `
  -Version "2022.1" `
  -SourceUri "https://example.gov.invalid/shanghai-base-data-2022" `
  -Actor "data-maintainer"
```

Supported MDB catalog keys:

- `shanghai.admin.town` -> `TOWN_CODE`
- `shanghai.population.city` -> `CITY_POPULATION`
- `shanghai.population.county` -> `COUNTY_POPULATION`
- `shanghai.population.town` -> `TOWN_POPULATION`
- `shanghai.building.city` -> `CITY_BUILDING`
- `shanghai.building.county` -> `COUNTY_BUILDING`
- `shanghai.building.town` -> `TOWN_BUILDING`
- `shanghai.economy.county` -> `economy`
- `shanghai.fault` -> `ACTIVEFAULT`

The host CLI returns the import job UUID. It populates the candidate, validates it, and either completes or rejects the job in the same transaction.

## Importing GDP And DEM GeoTIFF Files

GeoTIFF files are processed by `data-asset-worker` in the Linux container. The supported raster catalog keys are `shanghai.gdp.raster` and `shanghai.dem.raster`. These are optional background assets and are not loss formula inputs.

```powershell
$gdp = Get-Item "D:\地震应急辅助决策系统\基础数据\GDP_2020年.tif"
$form = @{
  version     = "2020.1"
  source_uri  = "https://example.gov.invalid/shanghai-gdp-2020"
  change_note = "initial GDP raster"
  file        = $gdp
}
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/data-assets/shanghai.gdp.raster/import" `
  -Method Post `
  -Headers $headers `
  -Form $form
```

Repeat for `shanghai.dem.raster`. The worker requires a valid numeric EPSG or ESRI authority code known to PostGIS, positive dimensions and band count, finite resolution, and valid bounds. It persists the raster in `data_asset_rasters` and verifies that the PostGIS export checksum, width, height, SRID, band metadata, nodata, and bounds match the source.

## Validating A Candidate Version

List candidate versions and copy the target `id`:

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/data-asset-versions?asset_key=shanghai.population.town" `
  -Headers $headers
```

Validate:

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/data-asset-versions/<version-id>/validate" `
  -Method Post `
  -Headers $headers
```

Validation checks data type, business keys, required fields, numeric types and bounds, geometry type, spatial extent, expected record count, raster metadata, and configured aggregate relationships. A publishable report has status `validated` and no errors. Warnings do not block publication.

## Publishing A Version

Only `superadmin` or `data_publisher` may publish. Publishing a validated version retires the current published version for the same asset automatically.

```powershell
$body = @{ reason = "approved for emergency use" } | ConvertTo-Json
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/data-asset-versions/<version-id>/publish" `
  -Method Post `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

Candidate versions cannot be used by an assessment until they are `published`. A correction or assessment run reads only versions in `data_asset_snapshots`, which are frozen at run creation.

## Retiring And Rolling Back A Version

Retire the current published version:

```powershell
$body = @{ reason = "data source withdrawn" } | ConvertTo-Json
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/data-asset-versions/<version-id>/retire" `
  -Method Post `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

Roll a retired version back to published:

```powershell
$body = @{ reason = "previous version restored" } | ConvertTo-Json
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/data-asset-versions/<version-id>/rollback" `
  -Method Post `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

Retiring and rolling back do not modify historical `data_asset_snapshots`. A previously created assessment run continues to reference the version and checksum that were frozen for that run.

## Inspecting Import Failures

A failed validation retains the import job, validation errors, warnings, and source checksum. Query the jobs:

```sql
SELECT id, file_name, file_format, status, raw_checksum,
       validation_errors, validation_warnings, error_summary,
       started_at, completed_at
FROM data_asset_import_jobs
ORDER BY created_at DESC
LIMIT 20;
```

Job states:

- `queued`: accepted but not yet claimed.
- `running`: worker claimed it.
- `completed`: candidate populated and validated successfully.
- `rejected`: validation produced errors.
- `failed`: normalization or persistence raised an exception.

Do not mutate `status` by hand to hide a failure. Re-import the corrected source as a new candidate version.

## Inspecting Evaluation Snapshots

Query versions and their live state:

```sql
SELECT da.asset_key, dav.version, dav.status, dav.quality_grade,
       dav.record_count, dav.checksum, dav.published_at
FROM data_assets da
JOIN data_asset_versions dav ON dav.asset_id = da.id
WHERE da.region_id = '<region-id>'
ORDER BY da.asset_key, dav.created_at DESC;
```

Query the frozen asset set for an assessment run:

```sql
SELECT das.asset_key, das.version, das.checksum, das.role, das.required
FROM data_asset_snapshots das
WHERE das.run_id = '<run-id>'
ORDER BY das.asset_key;
```

Query the records or raster attached to a version:

```sql
SELECT COUNT(*) AS record_count
FROM data_asset_records
WHERE version_id = '<version-id>';

SELECT ST_Width(rast), ST_Height(rast), ST_SRID(rast), checksum
FROM data_asset_rasters
WHERE version_id = '<version-id>';
```

## Update Overdue Detection

The asset summary endpoint calculates `update_due_at` from `published_at + update_interval_days` and marks `is_update_overdue` when that instant is now or earlier. No asset summary is emitted until the asset has a published version.

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/data-assets?region_id=shanghai" -Headers $headers
```

The frontend data asset page at `http://localhost:5173/data-assets` shows the overdue marker for affected assets.

## Backup And Restore

Backups must include PostgreSQL/PostGIS, especially `data_asset_records` and `data_asset_rasters`, and the host bind-mounted directory configured by `DATA_ASSET_STORAGE_HOST_DIR`.

PostgreSQL backup example:

```powershell
docker compose --env-file .env -f infra/compose.yaml exec postgres pg_dump `
  -U earthquake `
  -d earthquake `
  -Fc `
  -f /tmp/earthquake-data-asset.dump
```

Restore requires a restored database plus the matching host storage directory. Raw source files are content-addressed under `DATA_ASSET_STORAGE_ROOT`, so restoring the directory preserves the original bytes even if a candidate was rejected. After restore, verify the Alembic head, raster extension, published versions, and host directory before restarting the API and worker.

## Verification Commands

Automated verification must use a disposable database. The migration reversibility tests downgrade to revision `0009_non_cenc_lifecycle`, which drops the data asset tables and would erase an acceptance or production catalog.

Create and migrate the disposable database:

```powershell
docker compose --env-file .env -f infra/compose.yaml exec postgres `
  createdb -U earthquake earthquake_task9_verify

$values = @{}
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*([^#=]+)=(.*)$') {
    $values[$matches[1].Trim()] = $matches[2].Trim()
  }
}
$verificationDatabaseUrl = $values["DATABASE_URL"] -replace `
  '/earthquake$', '/earthquake_task9_verify'

docker compose --env-file .env -f infra/compose.yaml run --rm `
  -e "DATABASE_URL=$verificationDatabaseUrl" `
  api alembic upgrade head
```

Run the complete suite:

```powershell
docker compose --env-file .env -f infra/compose.yaml config --quiet
docker compose --env-file .env -f infra/compose.yaml run --rm `
  -e "DATABASE_URL=$verificationDatabaseUrl" `
  -v "$env:TEMP\temporal-cache\temporal-test-server_1.39.0_linux_amd64\temporal-test-server:/tmp/temporal-test-server-sdk-python-1.33.0" `
  api pytest -q
docker compose --env-file .env -f infra/compose.yaml run --rm api ruff check app tests
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm test
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run typecheck
docker compose --env-file .env -f infra/compose.yaml run --rm frontend npm run build
```

Task 9 verification on 2026-09-29:

- Compose configuration: exit `0`.
- Alembic: `0012_data_asset_center (head)`.
- Backend: `589 passed, 2 skipped, 2 warnings in 268.93s`.
- Ruff: `All checks passed!`.
- Frontend tests: `10` files passed, `44` tests passed.
- Typecheck: `tsc -b` exit `0`.
- Production build: `51 modules transformed`, Vite build succeeded.
- Placeholder scan: no matches.

Focused review verification for the domain, MDB importer, raster importer/repository, and data-asset permission routes passed `54` tests in `9.59s`.

The two backend warnings are third-party deprecations:

- `starlette/testclient.py:40`: `anyio.abc.BlockingPortal` is deprecated in favor of `anyio.from_thread.BlockingPortal`.
- `tests/test_intensity_artifacts.py::test_raster_codec_round_trip_preserves_values_and_georeference`: `rasterio/transform.py:178` requests `@` matrix multiplication instead of `*`.

Neither warning originates in the Task 9 data asset changes, and neither affects the passing result.

The Temporal SDK test server binary was mounted explicitly because the test container cannot download it in this offline/shared environment. A direct full-suite run against an already-populated acceptance database is not valid: migration reversibility tests downgrade the schema and real published assets collide with fixture assumptions.

## Task 9 Real-Data Acceptance

The supplied Shanghai MDB and rasters were imported through the host MDB CLI and HTTP import API, validated, and published on 2026-09-29.

### Required MDB Table Counts And Disposition

The source counts were read directly from the authoritative MDB with the Microsoft Access ODBC driver in read-only mode:

| Source table | Raw count | Task 9 disposition |
| --- | ---: | --- |
| `TOWN_CODE` | 212 | Counted; imported as `shanghai.admin.town@2022.1`, validated, and published with 212 records. |
| `TOWN_POPULATION` | 213 | Counted at source; imported as `shanghai.population.town@2022.1-r1`, validated, and published with 212 records after excluding `31012000000000`. |
| `TOWN_BUILDING` | 212 | Counted; imported as `shanghai.building.town@2022.1-r1`, validated, and published with 212 records. |
| `COUNTY_POPULATION` | 17 | Counted only; no Task 9 candidate version was imported or published. |
| `COUNTY_BUILDING` | 17 | Counted only; no Task 9 candidate version was imported or published. |
| `economy` | 17 | Counted; imported as `shanghai.economy.county@2022.1`, validated, and published with 17 records. |
| `ACTIVEFAULT` | 23 | Counted only; no Task 9 candidate version was imported or published. |

The count evidence was produced from the verified source file:

```powershell
$venv = Join-Path $env:TEMP "shdzj-task9-venv\Scripts\python.exe"
$code = @'
import pyodbc

path = r"D:\地震应急辅助决策系统\基础数据\上海应急基础数据2022.mdb"
conn = pyodbc.connect(
    f"DRIVER={{Microsoft Access Driver (*.mdb, *.accdb)}};"
    f"DBQ={path};ReadOnly=True;"
)
cur = conn.cursor()
for table in (
    "TOWN_CODE",
    "TOWN_POPULATION",
    "TOWN_BUILDING",
    "COUNTY_POPULATION",
    "COUNTY_BUILDING",
    "economy",
    "ACTIVEFAULT",
):
    cur.execute(f"SELECT COUNT(*) FROM [{table}]")
    raw = cur.fetchone()[0]
    if table == "TOWN_POPULATION":
        cur.execute(
            "SELECT COUNT(*) FROM [TOWN_POPULATION] WHERE [ID] <> ?",
            "31012000000000",
        )
        published = cur.fetchone()[0]
        print(f"{table}: raw={raw}, published={published}")
    else:
        print(f"{table}: raw={raw}")
cur.close()
conn.close()
'@
$code | & $venv -
```

Exact output:

```text
TOWN_CODE: raw=212
TOWN_POPULATION: raw=213, published=212
TOWN_BUILDING: raw=212
COUNTY_POPULATION: raw=17
COUNTY_BUILDING: raw=17
economy: raw=17
ACTIVEFAULT: raw=23
```

The raw MDB checksum matched the accepted source:

```text
46047e37f1de972affac23e3e1aaf1314f895c6d33d8c548bc464e93b0fd30ca
```

### Published Assets

| Asset | Published version | Records | Result |
| --- | --- | ---: | --- |
| `shanghai.admin.town` | `2022.1` | 212 | published |
| `shanghai.population.town` | `2022.1-r1` | 212 after excluding `31012000000000` | published |
| `shanghai.building.town` | `2022.1-r1` | 212 | published |
| `shanghai.economy.county` | `2022.1` | 17 | published |
| `shanghai.gdp.raster` | `2020.1-r3` | 4457 x 4496, SRID 102025 | published optional background |
| `shanghai.dem.raster` | `2023.1` | 15955 x 12402, SRID 4326 | published optional background |

### HIGH_RISE Contract Decision

Controller ruling: the specification requires a required-field contract, but it does not name `HIGH_RISE`. The authoritative `TOWN_BUILDING` source contains 212 rows and 212 null `HIGH_RISE` values, so `shanghai.building.town.HIGH_RISE` is optional for the first Shanghai catalog. This avoids inventing zero values. The other numerical and structural fields remain required, and the city/county building contracts are unchanged.

Reversal path: if authoritative `HIGH_RISE` values are supplied, set the town contract field back to required, update its regression expectation, and import a new immutable version through validation and publication. Existing assessment snapshots retain their prior version and checksum, so the contract change does not rewrite historical runs. Keeping the field required without source values would reject all 212 town-building rows and block publication.

The accepted GDP raster uses the PostGIS-known `ESRI:102025` authority. Its source-file checksum is `3dad0bbdd4203126b05cccfe7bbe546794de1a505a7b5462887f7cbeccaa80da`; the canonical PostGIS GTiff export checksum is `fb0ba2b8f837691dbef44d465d6b51023714a18a52eae749cf6ca4abed9ea5a1`.

The accepted DEM source and canonical PostGIS export checksum is `5b25f9019910ed14ade30241e4e7cd046e12f779083624878ffcf061fece5483`.

The GDP and DEM assets are optional background assets in `shanghai-required-assets.yaml`; they are not loss formula inputs.
