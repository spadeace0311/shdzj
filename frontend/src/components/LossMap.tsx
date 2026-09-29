import { useEffect, useMemo, useRef, useState } from "react";
import type { FeatureCollection, Geometry } from "geojson";
import type {
  Map as MaplibreMap,
  GeoJSONSourceSpecification,
  MapLayerMouseEvent,
  MapOptions,
  RasterSourceSpecification,
  RequestParameters,
} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

import { getAccessToken } from "../api/client";
import type { LossAreaFeature, LossGridArtifact } from "../types";

const BASEMAP_SOURCE = "loss-map-amap";
const BASEMAP_LAYER = "loss-map-amap-layer";
const FUSED_SOURCE = "loss-map-fused-intensity";
const FUSED_LAYER = "loss-map-fused-intensity-fill";
const TOWN_SOURCE = "loss-map-town-loss";
const TOWN_FILL_LAYER = "loss-map-town-fill";
const TOWN_LINE_LAYER = "loss-map-town-line";
const GRID_SOURCE = "loss-map-grid";
const GRID_LAYER = "loss-map-grid-raster";

interface LossMapProps {
  center: [number, number];
  tileUrlTemplate?: string;
  townFeatures: LossAreaFeature[];
  gridArtifact: LossGridArtifact | null;
  onTownSelect?: (townCode: string) => void;
  selectedTownCode?: string | null;
  selectedProductLabel?: string;
}

interface GeoJsonSourceLike {
  setData(data: unknown): void;
}

interface RasterSourceLike {
  setTiles(tiles: string[]): void;
}

function isSameOriginUrl(url: string): boolean {
  if (url.startsWith("/")) {
    return true;
  }
  try {
    return new URL(url, window.location.origin).origin === window.location.origin;
  } catch {
    return false;
  }
}

function expandTileTemplate(template: string, band: string): string {
  return template
    .replace("{band}", encodeURIComponent(band))
    .replace("{z}", "{z}")
    .replace("{x}", "{x}")
    .replace("{y}", "{y}");
}

function buildTownFeatureCollection(
  features: LossAreaFeature[],
): FeatureCollection<Geometry> {
  return {
    type: "FeatureCollection",
    features: features.map((feature) => ({
      type: "Feature" as const,
      id: feature.area_code,
      properties: {
        area_code: feature.area_code,
        area_name: feature.area_name,
      },
      geometry: feature.geometry as unknown as Geometry,
    })),
  };
}

export function LossMap({
  center,
  tileUrlTemplate,
  townFeatures,
  gridArtifact,
  onTownSelect,
  selectedTownCode,
  selectedProductLabel,
}: LossMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MaplibreMap | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const [showFusedIntensity, setShowFusedIntensity] = useState(true);
  const [showTownLoss, setShowTownLoss] = useState(true);
  const [showGrid, setShowGrid] = useState(true);
  const [selectedBand, setSelectedBand] = useState(
    gridArtifact?.bands[0]?.name ?? "",
  );

  useEffect(() => {
    if (
      gridArtifact &&
      !gridArtifact.bands.some((band) => band.name === selectedBand)
    ) {
      setSelectedBand(gridArtifact.bands[0]?.name ?? "");
    }
  }, [gridArtifact, selectedBand]);

  const onTownSelectRef = useRef(onTownSelect);
  useEffect(() => {
    onTownSelectRef.current = onTownSelect;
  }, [onTownSelect]);

  const transformRequest = useMemo(
    () =>
      (url: string): RequestParameters => {
        const token = getAccessToken();
        if (token && isSameOriginUrl(url)) {
          return {
            url,
            headers: {
              Authorization: `Bearer ${token}`,
            },
          };
        }
        return { url };
      },
    [],
  );

  useEffect(() => {
    if (!getAccessToken() || !containerRef.current || mapRef.current) {
      return;
    }
    let disposed = false;
    let map: MaplibreMap | null = null;

    const options: MapOptions = {
      container: containerRef.current,
      center: [center[1], center[0]],
      zoom: 9.5,
      minZoom: 6,
      maxZoom: 17,
      attributionControl: false,
      transformRequest,
      style: {
        version: 8,
        sources: {},
        layers: [
          {
            id: "loss-map-background",
            type: "background",
            paint: {
              "background-color": "#dfe6ec",
            },
          },
        ],
      },
    };

    void import("maplibre-gl").then(({ Map: MapConstructor }) => {
      if (disposed || !containerRef.current) {
        return;
      }
      const createdMap = new MapConstructor(options);
      map = createdMap;
      mapRef.current = createdMap;
      setMapReady(true);

      if (tileUrlTemplate) {
        createdMap.addSource(BASEMAP_SOURCE, {
          type: "raster",
          tiles: [tileUrlTemplate],
          tileSize: 256,
        } satisfies RasterSourceSpecification);
        createdMap.addLayer({
          id: BASEMAP_LAYER,
          type: "raster",
          source: BASEMAP_SOURCE,
          paint: {
            "raster-opacity": 0.95,
          },
        });
      }

      createdMap.addSource(FUSED_SOURCE, {
        type: "geojson",
        data: {
          type: "FeatureCollection",
          features: [],
        },
      } satisfies GeoJSONSourceSpecification);
      createdMap.addLayer({
        id: FUSED_LAYER,
        type: "fill",
        source: FUSED_SOURCE,
        layout: {
          visibility: showFusedIntensity ? "visible" : "none",
        },
        paint: {
          "fill-color": "#176b9b",
          "fill-opacity": 0.12,
        },
      });

      createdMap.on("click", TOWN_FILL_LAYER, (event: MapLayerMouseEvent) => {
        const feature = event.features?.[0];
        const townCode = feature?.properties?.area_code;
        if (typeof townCode === "string" && townCode.length > 0) {
          onTownSelectRef.current?.(townCode);
        }
      });
    });

    return () => {
      disposed = true;
      map?.remove();
      mapRef.current = null;
    };
    // MapLibre owns the container for the component lifetime; visibility
    // changes are synchronized through the focused effects below.
  }, [center, transformRequest]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) {
      return;
    }
    if (!map.getSource(TOWN_SOURCE)) {
      map.addSource(TOWN_SOURCE, {
        type: "geojson",
        data: buildTownFeatureCollection(townFeatures),
      } satisfies GeoJSONSourceSpecification);
      map.addLayer({
        id: TOWN_FILL_LAYER,
        type: "fill",
        source: TOWN_SOURCE,
        layout: {
          visibility: showTownLoss ? "visible" : "none",
        },
        paint: {
          "fill-color": [
            "case",
            ["==", ["get", "area_code"], selectedTownCode ?? ""],
            "#0d7a6f",
            "#2b6f9b",
          ],
          "fill-opacity": 0.3,
        },
      });
      map.addLayer({
        id: TOWN_LINE_LAYER,
        type: "line",
        source: TOWN_SOURCE,
        layout: {
          visibility: showTownLoss ? "visible" : "none",
        },
        paint: {
          "line-color": "#173d50",
          "line-width": 1.2,
        },
      });
    } else {
      (map.getSource(TOWN_SOURCE) as unknown as GeoJsonSourceLike).setData(
        buildTownFeatureCollection(townFeatures),
      );
    }
  }, [mapReady, selectedTownCode, showTownLoss, townFeatures]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) {
      return;
    }

    const token = getAccessToken();
    const tileTemplate =
      gridArtifact && selectedBand && token
        ? expandTileTemplate(gridArtifact.tile_template, selectedBand)
        : null;

    if (!tileTemplate) {
      if (map.getLayer(GRID_LAYER)) {
        map.removeLayer(GRID_LAYER);
      }
      if (map.getSource(GRID_SOURCE)) {
        map.removeSource(GRID_SOURCE);
      }
      return;
    }

    if (!map.getSource(GRID_SOURCE)) {
      map.addSource(GRID_SOURCE, {
        type: "raster",
        tiles: [tileTemplate],
        tileSize: 256,
      } satisfies RasterSourceSpecification);
      map.addLayer({
        id: GRID_LAYER,
        type: "raster",
        source: GRID_SOURCE,
        layout: {
          visibility: showGrid ? "visible" : "none",
        },
        paint: {
          "raster-opacity": 0.72,
        },
      });
      return;
    }

    (map.getSource(GRID_SOURCE) as unknown as RasterSourceLike).setTiles([
      tileTemplate,
    ]);
  }, [gridArtifact, mapReady, selectedBand, showGrid]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.getLayer(FUSED_LAYER)) {
      return;
    }
    map.setLayoutProperty(
      FUSED_LAYER,
      "visibility",
      showFusedIntensity ? "visible" : "none",
    );
  }, [mapReady, showFusedIntensity]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) {
      return;
    }
    const visibility = showTownLoss ? "visible" : "none";
    if (map.getLayer(TOWN_FILL_LAYER)) {
      map.setLayoutProperty(TOWN_FILL_LAYER, "visibility", visibility);
    }
    if (map.getLayer(TOWN_LINE_LAYER)) {
      map.setLayoutProperty(TOWN_LINE_LAYER, "visibility", visibility);
    }
  }, [mapReady, showTownLoss]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.getLayer(GRID_LAYER)) {
      return;
    }
    map.setLayoutProperty(
      GRID_LAYER,
      "visibility",
      showGrid ? "visible" : "none",
    );
  }, [mapReady, showGrid]);

  const selectedTown =
    townFeatures.find((feature) => feature.area_code === selectedTownCode)
      ?.area_name ?? selectedTownCode;

  return (
    <section className="loss-map" aria-label="损失空间分布">
      <div className="loss-map__toolbar">
        <label className="loss-map__toggle">
          <input
            type="checkbox"
            checked={showFusedIntensity}
            onChange={(event) => setShowFusedIntensity(event.target.checked)}
          />
          <span>融合烈度</span>
        </label>
        <label className="loss-map__toggle">
          <input
            type="checkbox"
            checked={showTownLoss}
            onChange={(event) => setShowTownLoss(event.target.checked)}
          />
          <span>街镇损失</span>
        </label>
        <label className="loss-map__toggle">
          <input
            type="checkbox"
            checked={showGrid}
            disabled={!gridArtifact}
            onChange={(event) => setShowGrid(event.target.checked)}
          />
          <span>公里格网为空间化估算</span>
        </label>
        {gridArtifact && gridArtifact.bands.length > 1 ? (
          <label className="loss-map__band">
            <span>格网指标</span>
            <select
              value={selectedBand}
              onChange={(event) => setSelectedBand(event.target.value)}
            >
              {gridArtifact.bands.map((band) => (
                <option key={band.name} value={band.name}>
                  {band.name}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <div className="loss-map__selection" aria-live="polite">
          {selectedProductLabel ? <span>产品：{selectedProductLabel}</span> : null}
          <span>街镇：{selectedTown || "未选择"}</span>
        </div>
      </div>
      <div className="loss-map__canvas" ref={containerRef} />
    </section>
  );
}
