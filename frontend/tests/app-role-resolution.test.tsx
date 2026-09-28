import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import App from "../src/App";
import {
  ApiError,
  clearAccessToken,
  getCurrentUser,
  listDataAssetVersions,
  listDataAssets,
  login,
} from "../src/api/client";
import type {
  CurrentUser,
  DataAssetSummary,
  DataAssetVersion,
} from "../src/types";

vi.mock("../src/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/client")>();
  return {
    ...actual,
    clearAccessToken: vi.fn(),
    getCurrentUser: vi.fn(),
    listDataAssets: vi.fn(),
    listDataAssetVersions: vi.fn(),
    login: vi.fn(),
    importDataAsset: vi.fn(),
    validateDataAssetVersion: vi.fn(),
    publishDataAssetVersion: vi.fn(),
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
  source_crs: "EPSG:4326",
  license_name: null,
  acquired_at: null,
  valid_from: null,
  valid_to: null,
  quality_grade: "L2",
  change_note: "initial",
  schema_summary: {},
  statistics: {},
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

const loginMock = vi.mocked(login);
const getCurrentUserMock = vi.mocked(getCurrentUser);
const clearAccessTokenMock = vi.mocked(clearAccessToken);
const listDataAssetsMock = vi.mocked(listDataAssets);
const listDataAssetVersionsMock = vi.mocked(listDataAssetVersions);

beforeEach(() => {
  loginMock.mockReset();
  getCurrentUserMock.mockReset();
  clearAccessTokenMock.mockReset();
  listDataAssetsMock.mockReset();
  listDataAssetVersionsMock.mockReset();
});

function fillAndSubmitLogin() {
  fireEvent.change(screen.getByLabelText("用户名"), {
    target: { value: "operator" },
  });
  fireEvent.change(screen.getByLabelText("密码"), {
    target: { value: "secret" },
  });
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
}

async function loginAndOpenDataAssets() {
  fillAndSubmitLogin();
  await waitFor(() => expect(loginMock).toHaveBeenCalledTimes(1));
  await openDataAssetsAfterLogin();
}

async function openDataAssetsAfterLogin() {
  const dataAssetsLink = await screen.findByRole("link", { name: "数据资产" });
  fireEvent.click(dataAssetsLink);
  await screen.findByRole("heading", { name: "数据资产中心" });
}

test("resolves /me before enabling privileged data-asset controls", async () => {
  loginMock.mockResolvedValue({ access_token: "token-1" });
  listDataAssetsMock.mockResolvedValue([asset]);
  listDataAssetVersionsMock.mockResolvedValue([version]);

  let resolveCurrentUser!: (value: CurrentUser) => void;
  getCurrentUserMock.mockImplementation(
    () =>
      new Promise<CurrentUser>((resolve) => {
        resolveCurrentUser = resolve;
      }),
  );

  render(<App />);
  fillAndSubmitLogin();

  await waitFor(() => expect(getCurrentUserMock).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole("heading", { name: "数据资产中心" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "停用" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "发布" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "回滚" })).not.toBeInTheDocument();

  resolveCurrentUser({
    username: "operator",
    role: "data_publisher",
    workgroup: null,
  });

  await openDataAssetsAfterLogin();
  expect(await screen.findByRole("button", { name: "停用" })).toBeInTheDocument();
  expect(getCurrentUserMock).toHaveBeenCalledTimes(1);
});

test("rejected /me clears the session and fails closed", async () => {
  loginMock.mockResolvedValue({ access_token: "token-1" });
  getCurrentUserMock.mockRejectedValue(
    new ApiError("Could not validate credentials", 401),
  );

  render(<App />);
  fillAndSubmitLogin();

  await waitFor(() => expect(clearAccessTokenMock).toHaveBeenCalledTimes(1));
  expect(screen.getByRole("button", { name: "登录" })).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "数据资产中心" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "发布" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "停用" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "回滚" })).not.toBeInTheDocument();
});

test.each(["", "viewer"])(
  "does not enable privileged controls when /me resolves role %j",
  async (role) => {
    loginMock.mockResolvedValue({ access_token: "token-1" });
    getCurrentUserMock.mockResolvedValue({
      username: "operator",
      role,
      workgroup: null,
    });
    listDataAssetsMock.mockResolvedValue([asset]);
    listDataAssetVersionsMock.mockResolvedValue([version]);

    render(<App />);
    await loginAndOpenDataAssets();

    expect(screen.queryByRole("button", { name: "导入" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "校验" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "发布" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "停用" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "回滚" })).not.toBeInTheDocument();
  },
);
