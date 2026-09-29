import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as maplibregl from "maplibre-gl";

import { buildLossTileRequest, LossMap } from "../src/components/LossMap";
import { LossAssessmentPanel } from "../src/components/LossAssessmentPanel";
import { clearAccessToken, setAccessToken } from "../src/api/client";
import type {
  LossAreaFeature,
  LossGridArtifact,
  LossProductSummary,
  LossResult,
} from "../src/types";

interface MockMap {
  options: Record<string, unknown>;
  sources: Map<string, Record<string, unknown>>;
  layers: Map<string, Record<string, unknown>>;
  layerOrder: string[];
  layoutProps: Map<string, Record<string, unknown>>;
  paintProps: Map<string, Record<string, unknown>>;
  events: Array<{
    type: string;
    layer?: string;
    handler: (event: unknown) => void;
  }>;
  removed: boolean;
  addSource: ReturnType<typeof vi.fn>;
  addLayer: ReturnType<typeof vi.fn>;
  getSource: ReturnType<typeof vi.fn>;
  getLayer: ReturnType<typeof vi.fn>;
  setLayoutProperty: ReturnType<typeof vi.fn>;
  setPaintProperty: ReturnType<typeof vi.fn>;
  removeLayer: ReturnType<typeof vi.fn>;
  removeSource: ReturnType<typeof vi.fn>;
  moveLayer: ReturnType<typeof vi.fn>;
  on: ReturnType<typeof vi.fn>;
  remove: ReturnType<typeof vi.fn>;
}

vi.mock("maplibre-gl", () => {
  const maps: MockMap[] = [];

  function createMap(options: Record<string, unknown>): MockMap {
    const sources = new Map<string, Record<string, unknown>>();
    const layers = new Map<string, Record<string, unknown>>();
    const layoutProps = new Map<string, Record<string, unknown>>();
    const paintProps = new Map<string, Record<string, unknown>>();
    const events: MockMap["events"] = [];
    const layerOrder: string[] = [];

    const map = {
      options,
      sources,
      layers,
      layerOrder,
      layoutProps,
      paintProps,
      events,
      removed: false,
      addSource: vi.fn((id: string, source: Record<string, unknown>) => {
        sources.set(id, {
          ...source,
          setData: vi.fn((data: unknown) => {
            sources.set(id, { ...(sources.get(id) ?? {}), data });
          }),
          setTiles: vi.fn((tiles: string[]) => {
            sources.set(id, { ...(sources.get(id) ?? {}), tiles });
          }),
        });
      }),
      addLayer: vi.fn(
        (layer: Record<string, unknown>, beforeId?: string) => {
          layers.set(layer.id as string, layer);
          if (beforeId && layerOrder.includes(beforeId)) {
            layerOrder.splice(layerOrder.indexOf(beforeId), 0, layer.id as string);
          } else {
            layerOrder.push(layer.id as string);
          }
        },
      ),
      getSource: vi.fn((id: string) => sources.get(id)),
      getLayer: vi.fn((id: string) => layers.get(id)),
      setLayoutProperty: vi.fn(
        (id: string, key: string, value: unknown) => {
          layoutProps.set(id, {
            ...(layoutProps.get(id) ?? {}),
            [key]: value,
          });
          const layer = layers.get(id);
          if (layer) {
            layers.set(id, {
              ...layer,
              layout: { ...(layer.layout as Record<string, unknown>), [key]: value },
            });
          }
        },
      ),
      setPaintProperty: vi.fn((id: string, key: string, value: unknown) => {
        paintProps.set(id, {
          ...(paintProps.get(id) ?? {}),
          [key]: value,
        });
        const layer = layers.get(id);
        if (layer) {
          layers.set(id, {
            ...layer,
            paint: { ...(layer.paint as Record<string, unknown>), [key]: value },
          });
        }
      }),
      removeLayer: vi.fn((id: string) => {
        layers.delete(id);
        layerOrder.splice(layerOrder.indexOf(id), 1);
      }),
      removeSource: vi.fn((id: string) => {
        sources.delete(id);
      }),
      moveLayer: vi.fn((id: string, beforeId?: string) => {
        const currentIndex = layerOrder.indexOf(id);
        if (currentIndex >= 0) {
          layerOrder.splice(currentIndex, 1);
        }
        if (!beforeId) {
          layerOrder.push(id);
          return;
        }
        const beforeIndex = layerOrder.indexOf(beforeId);
        layerOrder.splice(beforeIndex >= 0 ? beforeIndex : layerOrder.length, 0, id);
      }),
      on: vi.fn(
        (
          type: string,
          layerOrHandler: string | ((event: unknown) => void),
          maybeHandler?: (event: unknown) => void,
        ) => {
          const layer = typeof layerOrHandler === "string" ? layerOrHandler : undefined;
          const handler =
            typeof layerOrHandler === "function" ? layerOrHandler : maybeHandler;
          if (handler) {
            events.push({ type, layer, handler });
          }
        },
      ),
      remove: vi.fn(() => {
        map.removed = true;
        sources.clear();
        layers.clear();
        layerOrder.length = 0;
      }),
    };

    return map;
  }

  return {
    Map: vi.fn((options: Record<string, unknown>) => {
      const map = createMap(options);
      maps.push(map);
      return map;
    }),
    __maps: maps,
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

const gridArtifact: LossGridArtifact = {
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
};

function mapModule(): { __maps: MockMap[] } {
  return maplibregl as unknown as { __maps: MockMap[] };
}

async function renderMap(
  props: Partial<{
    center: [number, number];
    tileUrlTemplate: string;
    townFeatures: LossAreaFeature[];
    gridArtifact: LossGridArtifact | null;
    selectedTownCode: string | null;
    onTownSelect: (townCode: string) => void;
  }> = {},
) {
  setAccessToken("loss-map-test-token");
  const utils = render(
    <LossMap
      center={[31.2, 121.5]}
      tileUrlTemplate="http://localhost/tiles/{z}/{x}/{y}.png"
      townFeatures={[]}
      gridArtifact={null}
      {...props}
    />,
  );
  await waitFor(() => expect(mapModule().__maps.length).toBeGreaterThan(0));
  return {
    ...utils,
    map: mapModule().__maps.at(-1) as MockMap,
  };
}

beforeEach(() => {
  clearAccessToken();
  mapModule().__maps.length = 0;
});

it("removes the unavailable fused intensity control", () => {
  render(
    <LossMap
      center={[31.2, 121.5]}
      tileUrlTemplate="http://localhost/tiles/{z}/{x}/{y}.png"
      townFeatures={[]}
      gridArtifact={null}
    />,
  );

  expect(screen.queryByLabelText("融合烈度")).not.toBeInTheDocument();
});

it("labels the derived layer as spatialized estimate", () => {
  render(
    <LossMap
      center={[31.2, 121.5]}
      tileUrlTemplate="http://localhost/tiles/{z}/{x}/{y}.png"
      townFeatures={[]}
      gridArtifact={gridArtifact}
    />,
  );

  expect(screen.getByText("公里格网为空间化估算")).toBeInTheDocument();
});

it("toggles town loss visibility through setLayoutProperty", async () => {
  const { map } = await renderMap({ townFeatures: [townFeature] });

  fireEvent.click(screen.getByLabelText("街镇损失"));

  await waitFor(() =>
    expect(map.setLayoutProperty).toHaveBeenCalledWith(
      "loss-map-town-fill",
      "visibility",
      "none",
    ),
  );
});

it("toggles grid visibility through setLayoutProperty", async () => {
  const { map } = await renderMap({ gridArtifact });

  fireEvent.click(screen.getByLabelText("公里格网为空间化估算"));

  await waitFor(() =>
    expect(map.setLayoutProperty).toHaveBeenCalledWith(
      "loss-map-grid-raster",
      "visibility",
      "none",
    ),
  );
});

it("emits the clicked town code", async () => {
  const onTownSelect = vi.fn();
  const { map } = await renderMap({
    townFeatures: [townFeature],
    onTownSelect,
  });
  const clickEvent = map.events.find((event) => event.type === "click");

  clickEvent?.handler({
    features: [{ properties: { area_code: "310115001" } }],
  });

  expect(onTownSelect).toHaveBeenCalledWith("310115001");
});

it("authorizes only same-origin loss artifact tile requests", () => {
  setAccessToken("secret-token");

  expect(
    buildLossTileRequest(
      "/api/v1/assessments/runs/r1/loss/artifact/p1/band/0/0/0.png",
    ),
  ).toEqual({
    url: "/api/v1/assessments/runs/r1/loss/artifact/p1/band/0/0/0.png",
    headers: { Authorization: "Bearer secret-token" },
  });
  expect(buildLossTileRequest("/api/v1/events")).toEqual({
    url: "/api/v1/events",
  });
  expect(
    buildLossTileRequest("https://example.com/api/v1/loss/artifact/p1/band/0/0/0.png"),
  ).toEqual({
    url: "https://example.com/api/v1/loss/artifact/p1/band/0/0/0.png",
  });

  clearAccessToken();
  expect(
    buildLossTileRequest(
      "/api/v1/assessments/runs/r1/loss/artifact/p1/band/0/0/0.png",
    ),
  ).toEqual({
    url: "/api/v1/assessments/runs/r1/loss/artifact/p1/band/0/0/0.png",
  });
});

it("does not create a map or loss raster source without a token", () => {
  render(
    <LossMap
      center={[31.2, 121.5]}
      tileUrlTemplate="http://localhost/tiles/{z}/{x}/{y}.png"
      townFeatures={[]}
      gridArtifact={gridArtifact}
    />,
  );

  expect(mapModule().__maps).toHaveLength(0);
});

it("recreates and synchronizes the map after center changes", async () => {
  const { map: firstMap, rerender } = await renderMap({
    townFeatures: [townFeature],
  });

  rerender(
    <LossMap
      center={[31.3, 121.6]}
      tileUrlTemplate="http://localhost/tiles/{z}/{x}/{y}.png"
      townFeatures={[townFeature]}
      gridArtifact={null}
    />,
  );

  await waitFor(() => expect(mapModule().__maps).toHaveLength(2));
  const secondMap = mapModule().__maps[1];
  expect(firstMap.removed).toBe(true);
  await waitFor(() =>
    expect(secondMap.getLayer("loss-map-town-fill")).toBeTruthy(),
  );
});

it("updates town fill paint when the selected town changes", async () => {
  const { map, rerender } = await renderMap({
    townFeatures: [townFeature],
    selectedTownCode: null,
  });
  await waitFor(() => expect(map.getLayer("loss-map-town-fill")).toBeTruthy());

  rerender(
    <LossMap
      center={[31.2, 121.5]}
      tileUrlTemplate="http://localhost/tiles/{z}/{x}/{y}.png"
      townFeatures={[townFeature]}
      gridArtifact={null}
      selectedTownCode="310115001"
    />,
  );

  await waitFor(() =>
    expect(map.setPaintProperty).toHaveBeenCalledWith(
      "loss-map-town-fill",
      "fill-color",
      expect.anything(),
    ),
  );
  const paintCall = map.setPaintProperty.mock.calls
    .filter((call: unknown[]) => call[0] === "loss-map-town-fill")
    .at(-1);
  expect(paintCall?.[2]).toEqual([
    "case",
    ["==", ["get", "area_code"], "310115001"],
    "#0d7a6f",
    "#2b6f9b",
  ]);
});

it("keeps town layers above the grid raster", async () => {
  const { map } = await renderMap({
    townFeatures: [townFeature],
    gridArtifact,
  });

  await waitFor(() => expect(map.getLayer("loss-map-town-fill")).toBeTruthy());
  expect(map.layerOrder.indexOf("loss-map-grid-raster")).toBeLessThan(
    map.layerOrder.indexOf("loss-map-town-fill"),
  );
});

it("makes basemap tiles reactive to tileUrlTemplate changes", async () => {
  const { map, rerender } = await renderMap({
    tileUrlTemplate: "http://localhost/tiles/a/{z}/{x}/{y}.png",
  });

  rerender(
    <LossMap
      center={[31.2, 121.5]}
      tileUrlTemplate="http://localhost/tiles/b/{z}/{x}/{y}.png"
      townFeatures={[]}
      gridArtifact={null}
    />,
  );

  await waitFor(() =>
    expect(map.sources.get("loss-map-amap")?.tiles).toEqual([
      "http://localhost/tiles/b/{z}/{x}/{y}.png",
    ]),
  );
});

it("resets a stale selected product when run products change", async () => {
  const firstProduct: LossProductSummary = spatializedProduct(
    "p1",
    "building_damage",
  );
  const secondProduct: LossProductSummary = spatializedProduct(
    "p2",
    "casualties",
  );
  const { rerender } = render(
    <LossAssessmentPanel
      runId="run-1"
      result={makeLossResult([firstProduct, secondProduct])}
    />,
  );

  const select = screen.getByLabelText("空间化产品") as HTMLSelectElement;
  await waitFor(() => expect(select.value).toBe("p1"));

  rerender(
    <LossAssessmentPanel
      runId="run-2"
      result={makeLossResult([secondProduct])}
    />,
  );

  await waitFor(() =>
    expect((screen.getByLabelText("空间化产品") as HTMLSelectElement).value).toBe(
      "p2",
    ),
  );
});

function spatializedProduct(
  productId: string,
  productType: LossProductSummary["product_type"],
): LossProductSummary {
  return {
    product_id: productId,
    product_type: productType,
    status: "complete",
    quality_grade: "L2",
    calibration_status: "reference_uncalibrated",
    coverage_ratio: 1,
    partial_scope: false,
    needs_review: false,
    spatialized_estimate: true,
    algorithm_version: "test-v1",
    parameter_version: "test-v1",
    region_profile_version: "test-v1",
    output_checksum: "a".repeat(64),
    statistics: {},
    metrics: [],
    reason: null,
  };
}

function makeLossResult(products: LossProductSummary[]): LossResult {
  return {
    run_id: "run-1",
    event_id: "event-1",
    revision_id: "revision-1",
    effective_run_id: "run-1",
    is_fallback: false,
    products,
  };
}
