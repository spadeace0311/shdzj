import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import * as maplibregl from "maplibre-gl";

import { setAccessToken } from "../src/api/client";
import { LossMap } from "../src/components/LossMap";
import type { LossAreaFeature } from "../src/types";

vi.mock("maplibre-gl", () => {
  const addSource = vi.fn();
  const addLayer = vi.fn();
  const on = vi.fn();
  const remove = vi.fn();
  const map = {
    addSource,
    addLayer,
    on,
    remove,
    getSource: vi.fn(() => ({ setData: vi.fn(), setTiles: vi.fn() })),
    getLayer: vi.fn(() => undefined),
    removeLayer: vi.fn(),
    removeSource: vi.fn(),
  };

  return {
    Map: vi.fn(() => map),
    __map: map,
  };
});

const townFeature: LossAreaFeature = {
  area_scope: "town",
  area_code: "310115001",
  area_name: "陆家嘴街道",
  geometry: {
    type: "Polygon",
    coordinates: [
      [
        [121.4, 31.1],
        [121.6, 31.1],
        [121.6, 31.3],
        [121.4, 31.3],
        [121.4, 31.1],
      ],
    ],
  },
  metrics: [],
};

it("toggles fused intensity and town loss layers", () => {
  render(
    <LossMap
      center={[31.2, 121.5]}
      tileUrlTemplate="http://localhost/tiles/{z}/{x}/{y}.png"
      townFeatures={[]}
      gridArtifact={null}
    />,
  );
  const townToggle = screen.getByLabelText("街镇损失");
  fireEvent.click(townToggle);
  expect(townToggle).not.toBeChecked();
});

it("labels the derived layer as spatialized estimate", () => {
  render(
    <LossMap
      center={[31.2, 121.5]}
      tileUrlTemplate="http://localhost/tiles/{z}/{x}/{y}.png"
      townFeatures={[]}
      gridArtifact={{
        product_id: "p1",
        checksum: "a".repeat(64),
        width: 1,
        height: 1,
        srid: 32651,
        bbox: [121.4, 31.1, 121.6, 31.3],
        spatial_allocation_rule: "town-uniform-v1",
        coverage_ratio: 1,
        bands: [
          {
            name: "collapsed_area_m2",
            unit: "m2",
            precision: 2,
          },
        ],
        spatialized_estimate: true,
        tile_template:
          "/api/v1/assessments/runs/r1/loss/artifact/p1/{band}/{z}/{x}/{y}.png",
      }}
    />,
  );
  expect(screen.getByText("公里格网为空间化估算")).toBeInTheDocument();
});

it("emits the clicked town code", async () => {
  const onTownSelect = vi.fn();
  setAccessToken("loss-map-test-token");
  render(
    <LossMap
      center={[31.2, 121.5]}
      tileUrlTemplate="http://localhost/tiles/{z}/{x}/{y}.png"
      townFeatures={[townFeature]}
      gridArtifact={null}
      onTownSelect={onTownSelect}
    />,
  );

  const mapConstructor = (maplibregl as unknown as {
    Map: ReturnType<typeof vi.fn>;
  }).Map;
  await waitFor(() => expect(mapConstructor).toHaveBeenCalled());
  const clickCalls = mapConstructor.mock.results[0]?.value.on.mock.calls.filter(
    (call: unknown[]) => call[0] === "click",
  );
  const clickHandler = clickCalls.at(-1)?.[2] as (event: {
    features?: Array<{ properties?: { area_code?: string } }>;
  }) => void;

  clickHandler({
    features: [{ properties: { area_code: "310115001" } }],
  });

  expect(onTownSelect).toHaveBeenCalledWith("310115001");
});
