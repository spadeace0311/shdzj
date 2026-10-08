import { useEffect, useMemo, useRef, useState } from "react";
import type {
  GeoJSONSourceSpecification,
  LayerSpecification,
  Map as MaplibreMap,
  MapOptions,
  RasterSourceSpecification,
} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

import type { QaMapAction } from "../types";
import {
  applyQaMapAction,
  QA_LAYER_CATALOG,
  type QaActionMap,
  type QaLayerCatalog,
} from "../qa/mapActions";

interface QaMapProps {
  actions: QaMapAction[];
  epicenter?: [number, number];
}

const BASEMAP_SOURCE = "qa-amap";
const BASEMAP_LAYER = "qa-amap-layer";
const BACKGROUND_LAYER = "qa-background";

function emptyFeatureCollection(): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: [],
  };
}

function layerTypeForKey(key: string): "circle" | "line" | "fill" {
  if (key === "faults") {
    return "line";
  }
  if (key === "population" || key === "loss") {
    return "fill";
  }
  return "circle";
}

function catalogWithEpicenter(
  epicenter?: [number, number],
): QaLayerCatalog {
  if (!epicenter) {
    return QA_LAYER_CATALOG;
  }
  return {
    ...QA_LAYER_CATALOG,
    targets: {
      ...QA_LAYER_CATALOG.targets,
      "event:epicenter": {
        center: epicenter,
      },
    },
  };
}

export function QaMap({ actions, epicenter }: QaMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MaplibreMap | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const centerKey = epicenter ? `${epicenter[0]}|${epicenter[1]}` : "default";

  const catalog = useMemo(
    () => catalogWithEpicenter(epicenter),
    [epicenter],
  );

  useEffect(() => {
    if (!containerRef.current) {
      return;
    }

    let disposed = false;
    let map: MaplibreMap | null = null;
    const center: [number, number] = epicenter ?? [121.47, 31.23];
    const options: MapOptions = {
      container: containerRef.current,
      center: [center[0], center[1]],
      zoom: 9,
      minZoom: 4,
      maxZoom: 17,
      attributionControl: false,
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
    });

    return () => {
      disposed = true;
      map?.remove();
      mapRef.current = null;
      setMapReady(false);
    };
  }, [centerKey]);

  useEffect(() => {
    if (!mapReady) {
      return;
    }
    const map = mapRef.current;
    if (!map) {
      return;
    }

    const tileUrlTemplate = import.meta.env.VITE_AMAP_TILE_URL_TEMPLATE;
    if (!tileUrlTemplate) {
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
          "raster-opacity": 0.96,
        },
      });
    }
  }, [mapReady]);

  useEffect(() => {
    if (!mapReady) {
      return;
    }
    const map = mapRef.current;
    if (!map) {
      return;
    }

    for (const [key, layer] of Object.entries(QA_LAYER_CATALOG.layers)) {
      if (!map.getSource(layer.source)) {
        map.addSource(layer.source, {
          type: "geojson",
          data: emptyFeatureCollection(),
        } satisfies GeoJSONSourceSpecification);
      }
      if (!map.getLayer(layer.id)) {
        map.addLayer({
          id: layer.id,
          type: layerTypeForKey(key),
          source: layer.source,
          layout: {
            visibility: "visible",
          },
          paint: {
            "circle-radius": 5,
            "circle-color": "#176b9b",
          },
        } as LayerSpecification);
      }
    }
  }, [mapReady]);

  useEffect(() => {
    if (!mapReady) {
      return;
    }
    const map = mapRef.current;
    if (!map) {
      return;
    }
    for (const action of actions) {
      applyQaMapAction(map as unknown as QaActionMap, action, catalog);
    }
  }, [actions, catalog, mapReady]);

  return (
    <section className="qa-map" data-testid="qa-map" aria-label="智能问策地图">
      <div className="qa-map__canvas" ref={containerRef} />
    </section>
  );
}
