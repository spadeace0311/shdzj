import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import {
  importDataAsset,
  listDataAssetVersions,
  listDataAssets,
  publishDataAssetVersion,
  retireDataAssetVersion,
  rollbackDataAssetVersion,
} from "../src/api/client";
import { DataAssetsPage } from "../src/pages/DataAssetsPage";
import type { DataAssetSummary, DataAssetVersion } from "../src/types";

vi.mock("../src/api/client", () => {
  class ApiError extends Error {
    status: number;

    constructor(message: string, status: number) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }

  return {
    ApiError,
    importDataAsset: vi.fn(),
    listDataAssetVersions: vi.fn(),
    listDataAssets: vi.fn(),
    publishDataAssetVersion: vi.fn(),
    validateDataAssetVersion: vi.fn(),
    retireDataAssetVersion: vi.fn(),
    rollbackDataAssetVersion: vi.fn(),
  };
});

const asset: DataAssetSummary = {
  asset_key: "shanghai.admin.town",
  region_id: "shanghai",
  name: "上海市街镇边界",
  data_type: "vector",
  spatial_granularity: "town",
  responsibility_unit: "信息中心",
  update_interval_days: 365,
  is_core: true,
  published_version: "2022.1",
  published_at: "2026-09-28T00:00:00Z",
  update_due_at: "2027-09-28T00:00:00Z",
  is_update_overdue: false,
};

const version: DataAssetVersion = {
  id: "version-1",
  asset_key: asset.asset_key,
  region_id: "shanghai",
  version: "2022.1",
  status: "published",
  source_uri: "https://example.gov.invalid/town.geojson",
  license_name: null,
  acquired_at: null,
  valid_from: null,
  valid_to: null,
  quality_grade: "L2",
  change_note: "initial",
  schema_summary: {},
  record_count: 212,
  checksum: "a".repeat(64),
  imported_by: "operator",
  reviewed_by: "reviewer",
  imported_at: "2026-09-28T00:00:00Z",
  validated_at: "2026-09-28T00:01:00Z",
  published_at: "2026-09-28T00:02:00Z",
  retired_at: null,
  validation_errors: [],
  validation_warnings: [],
};

beforeEach(() => {
  vi.mocked(listDataAssets).mockReset();
  vi.mocked(listDataAssetVersions).mockReset();
  vi.mocked(importDataAsset).mockReset();
  vi.mocked(publishDataAssetVersion).mockReset();
  vi.mocked(retireDataAssetVersion).mockReset();
  vi.mocked(rollbackDataAssetVersion).mockReset();
});

test("renders assets and version lifecycle actions", async () => {
  vi.mocked(listDataAssets).mockResolvedValue([asset]);
  vi.mocked(listDataAssetVersions).mockResolvedValue([version]);

  render(<DataAssetsPage userRole="data_publisher" />);

  expect(await screen.findByText("上海市街镇边界")).toBeInTheDocument();
  expect(screen.getByText("2022.1")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "停用" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "回滚" })).toBeInTheDocument();
});

test("submits a file and shows queued import", async () => {
  vi.mocked(listDataAssets).mockResolvedValue([asset]);
  vi.mocked(listDataAssetVersions).mockResolvedValue([]);
  vi.mocked(importDataAsset).mockResolvedValue({
    job_id: "job-1",
    version_id: "version-1",
    asset_key: asset.asset_key,
    version: "2023.1",
    status: "queued",
  });

  render(<DataAssetsPage userRole="data_maintainer" />);
  await screen.findByText("上海市街镇边界");
  fireEvent.change(screen.getByLabelText("数据版本"), {
    target: { value: "2023.1" },
  });
  fireEvent.change(screen.getByLabelText("来源 URI"), {
    target: { value: "https://example.gov.invalid/town-2023.geojson" },
  });
  fireEvent.change(screen.getByLabelText("变更说明"), {
    target: { value: "2023 update" },
  });
  fireEvent.change(screen.getByLabelText("选择文件"), {
    target: { files: [new File(["{...}"], "town.geojson", { type: "application/geo+json" })] },
  });
  fireEvent.click(screen.getByRole("button", { name: "导入" }));

  await waitFor(() => expect(importDataAsset).toHaveBeenCalledTimes(1));
  expect(await screen.findByText("导入任务已进入队列")).toBeInTheDocument();
});

test("hides publish actions from data maintainers", async () => {
  vi.mocked(listDataAssets).mockResolvedValue([asset]);
  vi.mocked(listDataAssetVersions).mockResolvedValue([]);

  render(<DataAssetsPage userRole="data_maintainer" />);
  await screen.findByText("上海市街镇边界");

  expect(screen.queryByRole("button", { name: "发布" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "停用" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "回滚" })).not.toBeInTheDocument();
});

test("publish confirmation requires a non-empty reason and sends it", async () => {
  vi.mocked(listDataAssets).mockResolvedValue([asset]);
  vi.mocked(listDataAssetVersions).mockResolvedValue([version]);
  vi.mocked(publishDataAssetVersion).mockResolvedValue(version);

  render(<DataAssetsPage userRole="data_publisher" />);
  await screen.findByText("上海市街镇边界");

  fireEvent.click(screen.getByRole("button", { name: "发布" }));
  const reasonInput = screen.getByLabelText("操作原因");
  const confirmButton = screen.getByRole("button", { name: "确认" });

  expect(confirmButton).toBeDisabled();
  fireEvent.change(reasonInput, { target: { value: "   " } });
  expect(confirmButton).toBeDisabled();
  fireEvent.change(reasonInput, { target: { value: "年度例行发布" } });
  expect(confirmButton).toBeEnabled();
  fireEvent.click(confirmButton);

  await waitFor(() => expect(publishDataAssetVersion).toHaveBeenCalledTimes(1));
  expect(publishDataAssetVersion).toHaveBeenCalledWith(
    "version-1",
    "年度例行发布",
  );
});

test("retire confirmation requires a non-empty reason and sends it", async () => {
  vi.mocked(listDataAssets).mockResolvedValue([asset]);
  vi.mocked(listDataAssetVersions).mockResolvedValue([version]);
  vi.mocked(retireDataAssetVersion).mockResolvedValue(version);

  render(<DataAssetsPage userRole="data_publisher" />);
  await screen.findByText("上海市街镇边界");

  fireEvent.click(screen.getByRole("button", { name: "停用" }));
  const reasonInput = screen.getByLabelText("操作原因");
  const confirmButton = screen.getByRole("button", { name: "确认" });

  expect(confirmButton).toBeDisabled();
  fireEvent.change(reasonInput, { target: { value: "   " } });
  expect(confirmButton).toBeDisabled();
  fireEvent.change(reasonInput, { target: { value: "源数据已下线" } });
  fireEvent.click(confirmButton);

  await waitFor(() => expect(retireDataAssetVersion).toHaveBeenCalledTimes(1));
  expect(retireDataAssetVersion).toHaveBeenCalledWith(
    "version-1",
    "源数据已下线",
  );
});

test("rollback confirmation requires a non-empty reason and sends it", async () => {
  vi.mocked(listDataAssets).mockResolvedValue([asset]);
  vi.mocked(listDataAssetVersions).mockResolvedValue([version]);
  vi.mocked(rollbackDataAssetVersion).mockResolvedValue(version);

  render(<DataAssetsPage userRole="data_publisher" />);
  await screen.findByText("上海市街镇边界");

  fireEvent.click(screen.getByRole("button", { name: "回滚" }));
  const reasonInput = screen.getByLabelText("操作原因");
  const confirmButton = screen.getByRole("button", { name: "确认" });

  expect(confirmButton).toBeDisabled();
  fireEvent.change(reasonInput, { target: { value: "   " } });
  expect(confirmButton).toBeDisabled();
  fireEvent.change(reasonInput, { target: { value: "回退至稳定版本" } });
  fireEvent.click(confirmButton);

  await waitFor(() => expect(rollbackDataAssetVersion).toHaveBeenCalledTimes(1));
  expect(rollbackDataAssetVersion).toHaveBeenCalledWith(
    "version-1",
    "回退至稳定版本",
  );
});
