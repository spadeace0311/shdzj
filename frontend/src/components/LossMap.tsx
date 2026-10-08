import { useEffect, useMemo, useRef, useState } from "react";
import type { FeatureCollection, Geometry } from "geojson";
import type {
  Map as MaplibreMap,
  DataDrivenPropertyValueSpecification,
  GeoJSONSourceSpecification,
  MapLayerMouseEvent,
  MapOptions,
  RasterSourceSpecification,
  RequestParameters,
} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

import { getAccessToken } from "../api/client";
import {
  applyQaMapAction,
  QA_LAYER_CATALOG,
  type QaActionMap,
} from "../qa/mapActions";
import {
  isQaMapActionExpired,
  useMapActionConsumer,
  useMapActionEventId,
} from "../qa/MapActionContext";
import type {
  IntensityGridArtifact,
  LossAreaFeature,
  LossGridArtifact,
} from "../types";

const BASEMAP_SOURCE = "loss-map-amap";
const BASEMAP_LAYER = "loss-map-amap-layer";
const TOWN_SOURCE = "loss-map-town-loss";
const TOWN_FILL_LAYER = "loss-map-town-fill";
const TOWN_LINE_LAYER = "loss-map-town-line";
const GRID_SOURCE = "loss-map-grid";
const GRID_LAYER = "loss-map-grid-raster";
const FUSED_SOURCE = "loss-map-fused-intensity";
const FUSED_LAYER = "loss-map-fused-intensity-raster";
const BACKGROUND_LAYER = "loss-map-background";

interface LossMapProps {
  center: [number, number];
  tileUrlTemplate?: string;
  townFeatures: LossAreaFeature[];
  gridArtifact: LossGridArtifact | null;
  fusedIntensityArtifact: IntensityGridArtifact | null;
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

export function isLossArtifactTileUrl(url: string): boolean {
  try {
    const parsed = new URL(url, window.location.origin);
    return (
      parsed.origin === window.location.origin &&
      (parsed.pathname.includes("/loss/artifact/") ||
        parsed.pathname.includes("/intensity/artifact/"))
    );
  } catch {
    return false;
  }
}

export function buildLossTileRequest(url: string): RequestParameters {
  const token = getAccessToken();
  if (!token || !isLossArtifactTileUrl(url)) {
    return { url };
  }
  return {
    url,
    headers: {
      Authorization: `Bearer ${token}`,
    },
  };
}

function expandTileTemplate(template: string, band: string): string {
  return template
    .replace("{band}", encodeURIComponent(band))
    .replace("{z}", "{z}")
    .replace("{x}", "{x}")
    .replace("{y}", "{y}");
}

function townFillColor(
  selectedTownCode: string | null | undefined,
): DataDrivenPropertyValueSpecification<string> {
  return [
    "case",
    ["==", ["get", "area_code"], selectedTownCode ?? ""],
    "#0d7a6f",
    "#2b6f9b",
  ] as unknown as DataDrivenPropertyValueSpecification<string>;
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
  fusedIntensityArtifact,
  onTownSelect,
  selectedTownCode,
  selectedProductLabel,
}: LossMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MaplibreMap | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const [showTownLoss, setShowTownLoss] = useState(true);
  const [showGrid, setShowGrid] = useState(true);
  const [showFusedIntensity, setShowFusedIntensity] = useState(true);
  const [selectedBand, setSelectedBand] = useState(
    gridArtifact?.bands[0]?.name ?? "",
  );
  const [fusedBand, setFusedBand] = useState(
    fusedIntensityArtifact?.bands[0]?.name ?? "",
  );
  const [lastActionType, setLastActionType] = useState("");
  const [lastActionError, setLastActionError] = useState("");
  const currentEventId = useMapActionEventId();
  const { eventId: actionEventId, action: latestAction } =
    useMapActionConsumer();
  const centerKey = center.join("|");

  useEffect(() => {
    if (
      !latestAction ||
      isQaMapActionExpired(latestAction)
    ) {
      return;
    }

    if (actionEventId !== currentEventId) {
      setLastActionType("");
      setLastActionError("");
      return;
    }
    if (!mapRef.current || !mapReady) {
      return;
    }
    setLastActionType("");
    setLastActionError("");
    try {
      const applied = applyQaMapAction(
        mapRef.current as unknown as QaActionMap,
        latestAction,
        QA_LAYER_CATALOG,
      );
      if (applied) {
        setLastActionType(latestAction.action_type);
      } else {
        setLastActionError("地图动作未执行");
      }
    } catch {
      setLastActionError("地图动作执行失败");
    }
  }, [actionEventId, currentEventId, latestAction, mapReady]);

  useEffect(() => {
    if (
      gridArtifact &&
      !gridArtifact.bands.some((band) => band.name === selectedBand)
    ) {
      setSelectedBand(gridArtifact.bands[0]?.name ?? "");
    }
  }, [gridArtifact, selectedBand]);

  useEffect(() => {
    if (
      fusedIntensityArtifact &&
      !fusedIntensityArtifact.bands.some((band) => band.name === fusedBand)
    ) {
      setFusedBand(fusedIntensityArtifact.bands[0]?.name ?? "");
    }
  }, [fusedIntensityArtifact, fusedBand]);

  const onTownSelectRef = useRef(onTownSelect);
  useEffect(() => {
    onTownSelectRef.current = onTownSelect;
  }, [onTownSelect]);

  const transformRequest = useMemo(
    () => (url: string): RequestParameters => buildLossTileRequest(url),
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
            id: BACKGROUND_LAYER,
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
      setMapReady(false);
    };
  }, [centerKey, transformRequest]);

  useEffect(() => {
    if (!mapReady) {
      return;
    }
    const map = mapRef.current;
    if (!map) {
      return;
    }

    if (!tileUrlTemplate) {
      if (map.getLayer(BASEMAP_LAYER)) {
        map.removeLayer(BASEMAP_LAYER);
      }
      if (map.getSource(BASEMAP_SOURCE)) {
        map.removeSource(BASEMAP_SOURCE);
      }
      return;
    }

    if (!map.getSource(BASEMAP_SOURCE)) {
      map.addSource(BASEMAP_SOURCE, {
        type: "raster",
        tiles: [tileUrlTemplate],
        tileSize: 256,
      } satisfies RasterSourceSpecification);
      map.addLayer({
        id: BASEMAP_LAYER,
        type: "raster",
        source: BASEMAP_SOURCE,
        paint: {
          "raster-opacity": 0.95,
        },
      });
      return;
    }

    (map.getSource(BASEMAP_SOURCE) as unknown as RasterSourceLike).setTiles([
      tileUrlTemplate,
    ]);
  }, [mapReady, tileUrlTemplate]);

  useEffect(() => {
    if (!mapReady) {
      return;
    }
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
      map.addLayer(
        {
          id: GRID_LAYER,
          type: "raster",
          source: GRID_SOURCE,
          layout: {
            visibility: showGrid ? "visible" : "none",
          },
          paint: {
            "raster-opacity": 0.72,
          },
        },
        map.getLayer(TOWN_FILL_LAYER) ? TOWN_FILL_LAYER : undefined,
      );
      return;
    }

    (map.getSource(GRID_SOURCE) as unknown as RasterSourceLike).setTiles([
      tileTemplate,
    ]);
  }, [gridArtifact, mapReady, selectedBand, showGrid]);

  useEffect(() => {
    if (!mapReady) {
      return;
    }
    const map = mapRef.current;
    if (!map) {
      return;
    }

    const token = getAccessToken();
    const tileTemplate =
      fusedIntensityArtifact && fusedBand && token
        ? expandTileTemplate(fusedIntensityArtifact.tile_template, fusedBand)
        : null;

    if (!tileTemplate) {
      if (map.getLayer(FUSED_LAYER)) {
        map.removeLayer(FUSED_LAYER);
      }
      if (map.getSource(FUSED_SOURCE)) {
        map.removeSource(FUSED_SOURCE);
      }
      return;
    }

    if (!map.getSource(FUSED_SOURCE)) {
      map.addSource(FUSED_SOURCE, {
        type: "raster",
        tiles: [tileTemplate],
        tileSize: 256,
      } satisfies RasterSourceSpecification);
    } else {
      (map.getSource(FUSED_SOURCE) as unknown as RasterSourceLike).setTiles([
        tileTemplate,
      ]);
    }

    if (!map.getLayer(FUSED_LAYER)) {
      map.addLayer(
        {
          id: FUSED_LAYER,
          type: "raster",
          source: FUSED_SOURCE,
          layout: {
            visibility: showFusedIntensity ? "visible" : "none",
          },
          paint: {
            "raster-opacity": 0.68,
          },
        },
        map.getLayer(GRID_LAYER) ? GRID_LAYER : undefined,
      );
    }
  }, [fusedIntensityArtifact, fusedBand, mapReady, showFusedIntensity]);

  useEffect(() => {
    if (!mapReady) {
      return;
    }
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
          "fill-color": townFillColor(selectedTownCode),
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
      return;
    }

    (map.getSource(TOWN_SOURCE) as unknown as GeoJsonSourceLike).setData(
      buildTownFeatureCollection(townFeatures),
    );
  }, [mapReady, selectedTownCode, showTownLoss, townFeatures]);

  useEffect(() => {
    if (!mapReady) {
      return;
    }
    const map = mapRef.current;
    if (!map || !map.getLayer(TOWN_FILL_LAYER)) {
      return;
    }
    map.setPaintProperty(
      TOWN_FILL_LAYER,
      "fill-color",
      townFillColor(selectedTownCode),
    );
  }, [mapReady, selectedTownCode]);

  useEffect(() => {
    if (!mapReady) {
      return;
    }
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
    if (!mapReady) {
      return;
    }
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

  useEffect(() => {
    if (!mapReady) {
      return;
    }
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

  const selectedTown =
    townFeatures.find((feature) => feature.area_code === selectedTownCode)
      ?.area_name ?? selectedTownCode;

  return (
    <section className="loss-map" aria-label="损失空间分布">
      <div className="loss-map__toolbar">
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
        {fusedIntensityArtifact ? (
          <label className="loss-map__toggle">
            <input
              type="checkbox"
              checked={showFusedIntensity}
              onChange={(event) => setShowFusedIntensity(event.target.checked)}
            />
            <span>融合烈度</span>
          </label>
        ) : null}
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
        {fusedIntensityArtifact && fusedIntensityArtifact.bands.length > 1 ? (
          <label className="loss-map__band">
            <span>融合烈度指标</span>
            <select
              value={fusedBand}
              onChange={(event) => setFusedBand(event.target.value)}
            >
              {fusedIntensityArtifact.bands.map((band) => (
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
          <span
            className="loss-map__last-action"
            data-testid="loss-map-last-action"
          >
            {lastActionType}
          </span>
          {lastActionError ? (
            <span
              className="loss-map__action-error"
              data-testid="loss-map-action-error"
              role="alert"
            >
              {lastActionError}
            </span>
          ) : null}
        </div>
      </div>
      <div className="loss-map__canvas" ref={containerRef} />
    </section>
  );
}
