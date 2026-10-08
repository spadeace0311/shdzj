import type { GeoJSONSourceSpecification, LayerSpecification } from "maplibre-gl";
import type { QaMapAction } from "../types";

export interface QaMapTarget {
  center: [number, number];
}

export interface QaCatalogLayer {
  id: string;
  source: string;
  featureIdProperty: string;
}

export interface QaLayerCatalog {
  targets: Record<string, QaMapTarget>;
  layers: Record<string, QaCatalogLayer>;
}

export interface QaActionMap {
  getZoom?: () => number;
  flyTo: (options: { center: [number, number]; zoom: number }) => void;
  fitBounds: (
    bounds: [[number, number], [number, number]],
    options?: { padding: number },
  ) => void;
  getSource: (id: string) => unknown;
  addSource: (id: string, source: GeoJSONSourceSpecification) => void;
  getLayer: (id: string) => unknown;
  addLayer: (layer: LayerSpecification) => void;
  setFilter: (layerId: string, filter?: unknown) => unknown;
  setLayoutProperty: (
    layerId: string,
    name: "visibility",
    value: "visible" | "none",
  ) => void;
}

interface GeoJsonSourceLike {
  setData(data: unknown): void;
}

const BUFFER_SOURCE_ID = "qa-buffer";
const BUFFER_LAYER_ID = "qa-buffer-layer";
const UNSAFE_TEXT = ["<", ">", "http://", "https://", "javascript:", "data:"];

const ALLOWED_LAYER_IDS = new Set([
  "epicenter",
  "faults",
  "historical_earthquakes",
  "population",
  "intensity",
  "loss",
  "artifacts",
]);

export const QA_LAYER_CATALOG: QaLayerCatalog = {
  targets: {},
  layers: {
    epicenter: {
      id: "qa-epicenter",
      source: "qa-epicenter-source",
      featureIdProperty: "event_id",
    },
    faults: {
      id: "qa-faults",
      source: "qa-faults-source",
      featureIdProperty: "fault_key",
    },
    historical_earthquakes: {
      id: "qa-historical-earthquakes",
      source: "qa-historical-earthquakes-source",
      featureIdProperty: "historical_event_id",
    },
    population: {
      id: "qa-population",
      source: "qa-population-source",
      featureIdProperty: "area_code",
    },
    intensity: {
      id: "qa-intensity",
      source: "qa-intensity-source",
      featureIdProperty: "event_id",
    },
    loss: {
      id: "qa-loss",
      source: "qa-loss-source",
      featureIdProperty: "area_code",
    },
    artifacts: {
      id: "qa-artifacts",
      source: "qa-artifacts-source",
      featureIdProperty: "artifact_id",
    },
  },
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function safeText(value: unknown): value is string {
  if (typeof value !== "string" || value.trim().length === 0) {
    return false;
  }
  const lowered = value.toLowerCase();
  return !UNSAFE_TEXT.some((marker) => lowered.includes(marker));
}

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function validLongitude(value: number): boolean {
  return value >= -180 && value <= 180;
}

function validLatitude(value: number): boolean {
  return value >= -90 && value <= 90;
}

function actionBase(value: unknown): value is QaMapAction {
  if (
    !isRecord(value) ||
    !safeText(value.reason) ||
    !safeText(value.valid_until)
  ) {
    return false;
  }
  return true;
}

function targetCenter(
  catalog: QaLayerCatalog,
  targetRef: string,
): [number, number] | null {
  const target = catalog.targets[targetRef];
  if (!target) {
    return null;
  }
  const [longitude, latitude] = target.center;
  if (
    !finiteNumber(longitude) ||
    !finiteNumber(latitude) ||
    !validLongitude(longitude) ||
    !validLatitude(latitude)
  ) {
    return null;
  }
  return [longitude, latitude];
}

function isLocateAction(value: QaMapAction): value is Extract<
  QaMapAction,
  { action_type: "locate" }
> {
  return (
    value.action_type === "locate" &&
    safeText(value.target_ref) &&
    actionBase(value)
  );
}

function isFitBoundsAction(value: QaMapAction): value is Extract<
  QaMapAction,
  { action_type: "fit_bounds" }
> {
  if (value.action_type !== "fit_bounds" || !actionBase(value)) {
    return false;
  }
  const bounds = value.bounds;
  if (
    !Array.isArray(bounds) ||
    bounds.length !== 4 ||
    !bounds.every(finiteNumber)
  ) {
    return false;
  }
  const [west, south, east, north] = bounds as number[];
  return (
    validLongitude(west) &&
    validLongitude(east) &&
    validLatitude(south) &&
    validLatitude(north) &&
    west < east &&
    south < north
  );
}

function isBufferAction(value: QaMapAction): value is Extract<
  QaMapAction,
  { action_type: "buffer" }
> {
  return (
    value.action_type === "buffer" &&
    safeText(value.target_ref) &&
    finiteNumber(value.radius_km) &&
    value.radius_km >= 0.1 &&
    value.radius_km <= 500 &&
    actionBase(value)
  );
}

function isHighlightAction(value: QaMapAction): value is Extract<
  QaMapAction,
  { action_type: "highlight" }
> {
  return (
    value.action_type === "highlight" &&
    safeText(value.target_ref) &&
    safeText(value.layer_id) &&
    ALLOWED_LAYER_IDS.has(value.layer_id) &&
    actionBase(value)
  );
}

function isSetLayersAction(value: QaMapAction): value is Extract<
  QaMapAction,
  { action_type: "set_layers" }
> {
  return (
    value.action_type === "set_layers" &&
    Array.isArray(value.layers) &&
    value.layers.length > 0 &&
    value.layers.every(
      (layer) => typeof layer === "string" && ALLOWED_LAYER_IDS.has(layer),
    ) &&
    actionBase(value)
  );
}

function referenceFor(targetRef: string): string {
  const separator = targetRef.indexOf(":");
  return separator === -1 ? targetRef : targetRef.slice(separator + 1);
}

function circleFeature(
  center: [number, number],
  radiusKm: number,
): GeoJSON.Feature<GeoJSON.Polygon> {
  const longitude = center[0];
  const latitude = center[1];
  const earthRadiusKm = 6371.0088;
  const angularRadius = radiusKm / earthRadiusKm;
  const points = 64;
  const coordinates: [number, number][] = [];

  for (let index = 0; index < points; index += 1) {
    const bearing = (index / points) * Math.PI * 2;
    const pointLatitude = Math.asin(
      Math.sin(latitude) * Math.cos(angularRadius) +
        Math.cos(latitude) *
          Math.sin(angularRadius) *
          Math.cos(bearing),
    );
    const pointLongitude =
      longitude +
      Math.atan2(
        Math.sin(bearing) *
          Math.sin(angularRadius) *
          Math.cos(latitude),
        Math.cos(angularRadius) -
          Math.sin(latitude) * Math.sin(pointLatitude),
      );
    coordinates.push([
      ((pointLongitude + 540) % 360) - 180,
      pointLatitude,
    ]);
  }
  coordinates.push(coordinates[0]!);

  return {
    type: "Feature",
    properties: {},
    geometry: {
      type: "Polygon",
      coordinates: [coordinates],
    },
  };
}

export function applyQaMapAction(
  map: QaActionMap,
  action: QaMapAction,
  layerCatalog: QaLayerCatalog,
): void {
  if (!isRecord(action) || !actionBase(action)) {
    return;
  }

  if (isLocateAction(action)) {
    const center = targetCenter(layerCatalog, action.target_ref);
    if (!center) {
      return;
    }
    const zoom = Math.max(8, map.getZoom?.() ?? 8);
    map.flyTo({ center, zoom });
    return;
  }

  if (isFitBoundsAction(action)) {
    const [west, south, east, north] = action.bounds;
    map.fitBounds(
      [
        [west, south],
        [east, north],
      ],
      { padding: 40 },
    );
    return;
  }

  if (isBufferAction(action)) {
    const center = targetCenter(layerCatalog, action.target_ref);
    if (!center) {
      return;
    }
    const feature = circleFeature(center, action.radius_km);
    const data: GeoJSON.FeatureCollection<GeoJSON.Polygon> = {
      type: "FeatureCollection",
      features: [feature],
    };
    const existingSource = map.getSource(BUFFER_SOURCE_ID);
    if (existingSource && "setData" in (existingSource as object)) {
      (existingSource as unknown as GeoJsonSourceLike).setData(data);
    } else {
      map.addSource(BUFFER_SOURCE_ID, {
        type: "geojson",
        data,
      } satisfies GeoJSONSourceSpecification);
      map.addLayer({
        id: BUFFER_LAYER_ID,
        type: "fill",
        source: BUFFER_SOURCE_ID,
        paint: {
          "fill-color": "#176b9b",
          "fill-opacity": 0.18,
        },
      } satisfies LayerSpecification);
    }
    return;
  }

  if (isHighlightAction(action)) {
    const layer = layerCatalog.layers[action.layer_id];
    if (!layer || !map.getLayer(layer.id)) {
      return;
    }
    map.setFilter(layer.id, [
      "==",
      ["get", layer.featureIdProperty],
      referenceFor(action.target_ref),
    ]);
    return;
  }

  if (isSetLayersAction(action)) {
    const requested = new Set(action.layers);
    for (const layer of Object.values(layerCatalog.layers)) {
      if (!map.getLayer(layer.id)) {
        continue;
      }
      map.setLayoutProperty(
        layer.id,
        "visibility",
        requested.has(thisLayerKey(layerCatalog, layer)) ? "visible" : "none",
      );
    }
  }
}

function thisLayerKey(
  catalog: QaLayerCatalog,
  layer: QaCatalogLayer,
): string {
  for (const [key, candidate] of Object.entries(catalog.layers)) {
    if (candidate.id === layer.id) {
      return key;
    }
  }
  return layer.id;
}
