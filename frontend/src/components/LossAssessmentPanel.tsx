import { useEffect, useState } from "react";

import {
  getAccessToken,
  getIntensityArtifact,
  getLossAreas,
  getLossArtifact,
} from "../api/client";
import { LossMap } from "./LossMap";
import type {
  LossCalibrationStatus,
  IntensityGridArtifact,
  LossAreaFeature,
  LossGridArtifact,
  LossProductSummary,
  LossProductType,
  LossResult,
  LossValue,
  LossValueType,
} from "../types";

interface LossAssessmentPanelProps {
  runId: string;
  result: LossResult;
  fusedIntensityProductId?: string | null;
}

interface MetricRow {
  id: string;
  areaScope: string;
  areaCode: string;
  areaName: string | null;
  metricKey: string;
  unit: string;
  precision: number | null;
  values: Partial<Record<LossValueType, LossValue>>;
}

const PRODUCT_LABELS: Record<LossProductType, string> = {
  building_damage: "建筑破坏",
  population_impact: "受灾人口与安置",
  casualties: "人员伤亡与压埋",
  economic_loss: "直接经济损失",
  resource_demand: "应急资源需求",
  validation: "结果校验",
};

const CALIBRATION_LABELS: Record<LossCalibrationStatus, string> = {
  calibrated: "已本地校准",
  reference_uncalibrated: "参考参数未本地校准",
  uncalibrated: "未本地校准",
};

const METRIC_LABELS: Record<string, string> = {
  total_area_m2: "破坏总面积",
  severe_or_collapsed_area_m2: "严重及倒塌面积",
  collapsed_area_m2: "倒塌面积",
  full_population: "全覆盖人口",
  affected_population: "受灾人口",
  emergency_shelter_population: "应急安置人口",
  temporary_shelter_population: "临时安置人口",
  deaths: "死亡人数",
  injuries: "受伤人数",
  buried: "压埋人数",
  reconstruction_loss_yuan: "房屋重建损失",
  contents_loss_yuan: "室内财产损失",
  total_loss_yuan: "直接经济损失",
};

const RESOURCE_LABELS: Record<string, string> = {
  rescue_team: "救援队伍",
  medical_team: "医疗队伍",
  epidemic_team: "防疫队伍",
  tent: "帐篷",
  drinking_water: "饮用水",
  toilet: "移动厕所",
  clothing: "衣物",
  quilt: "棉被",
  food: "食品",
  blanket: "毛毯",
  stretcher: "担架",
  sickbed: "病床",
};

const DEFAULT_SHANGHAI_CENTER: [number, number] = [31.2, 121.5];

function selectSpatializedProduct(
  products: LossProductSummary[],
): LossProductSummary | null {
  const candidates = [...products]
    .filter(
      (product) =>
        product.status === "complete" && product.spatialized_estimate,
    )
    .sort((left, right) => left.product_type.localeCompare(right.product_type));
  return candidates[0] ?? null;
}

export function LossAssessmentPanel({
  runId,
  result,
  fusedIntensityProductId,
}: LossAssessmentPanelProps) {
  const spatializedProducts = result.products.filter(
    (product) => product.spatialized_estimate,
  );
  const [selectedProductId, setSelectedProductId] = useState<string | null>(
    () => selectSpatializedProduct(result.products)?.product_id ?? null,
  );
  const [selectedTownCode, setSelectedTownCode] = useState<string | null>(null);
  const [townFeatures, setTownFeatures] = useState<LossAreaFeature[]>([]);
  const [gridArtifact, setGridArtifact] = useState<LossGridArtifact | null>(
    null,
  );
  const [fusedIntensityArtifact, setFusedIntensityArtifact] =
    useState<IntensityGridArtifact | null>(null);
  const [areaLoadFailed, setAreaLoadFailed] = useState(false);
  const [artifactLoadFailed, setArtifactLoadFailed] = useState(false);
  const [fusedArtifactLoadFailed, setFusedArtifactLoadFailed] = useState(false);

  const defaultProductId =
    selectSpatializedProduct(result.products)?.product_id ?? null;

  useEffect(() => {
    setSelectedProductId(defaultProductId);
  }, [defaultProductId, runId]);

  useEffect(() => {
    setSelectedTownCode(null);
  }, [runId]);

  useEffect(() => {
    if (!getAccessToken()) {
      return;
    }
    let active = true;
    setAreaLoadFailed(false);
    getLossAreas(runId, "town")
      .then((response) => {
        if (active) {
          setTownFeatures(response.features);
        }
      })
      .catch(() => {
        if (active) {
          setTownFeatures([]);
          setAreaLoadFailed(true);
        }
      });
    return () => {
      active = false;
    };
  }, [runId]);

  useEffect(() => {
    if (!getAccessToken()) {
      return;
    }
    if (!selectedProductId) {
      setGridArtifact(null);
      setArtifactLoadFailed(false);
      return;
    }

    let active = true;
    setGridArtifact(null);
    setArtifactLoadFailed(false);
    getLossArtifact(runId, selectedProductId)
      .then((artifact) => {
        if (active) {
          setGridArtifact(artifact);
        }
      })
      .catch(() => {
        if (active) {
          setGridArtifact(null);
          setArtifactLoadFailed(true);
        }
      });
    return () => {
      active = false;
    };
  }, [runId, selectedProductId]);

  useEffect(() => {
    if (!getAccessToken()) {
      return;
    }
    if (!fusedIntensityProductId) {
      setFusedIntensityArtifact(null);
      setFusedArtifactLoadFailed(false);
      return;
    }

    let active = true;
    setFusedIntensityArtifact(null);
    setFusedArtifactLoadFailed(false);
    getIntensityArtifact(runId, fusedIntensityProductId)
      .then((artifact) => {
        if (active) {
          setFusedIntensityArtifact(artifact);
        }
      })
      .catch(() => {
        if (active) {
          setFusedIntensityArtifact(null);
          setFusedArtifactLoadFailed(true);
        }
      });
    return () => {
      active = false;
    };
  }, [fusedIntensityProductId, runId]);

  const selectedProduct = spatializedProducts.find(
    (product) => product.product_id === selectedProductId,
  );
  const tileUrlTemplate = import.meta.env
    .VITE_AMAP_TILE_URL_TEMPLATE as string | undefined;

  return (
    <section
      className="loss-assessment"
      aria-labelledby="loss-assessment-title"
    >
      <header className="loss-assessment__header">
        <div>
          <p className="eyebrow">损失评估</p>
          <h2 id="loss-assessment-title">损失评估结果</h2>
        </div>
        <div className="loss-assessment__run">
          <span className="loss-assessment__run-label">运行编号</span>
          <span className="mono">{result.effective_run_id ?? runId}</span>
          {result.is_fallback ? (
            <span className="loss-assessment__fallback">回退结果</span>
          ) : null}
        </div>
      </header>

      <div className="loss-map-block">
        <div className="loss-map-block__controls">
          <label className="loss-map-product">
            <span>空间化产品</span>
            <select
              value={selectedProductId ?? ""}
              onChange={(event) => setSelectedProductId(event.target.value)}
            >
              <option value="" disabled>
                请选择空间化产品
              </option>
              {spatializedProducts.map((product) => (
                <option key={product.product_id} value={product.product_id}>
                  {PRODUCT_LABELS[product.product_type] ?? product.product_type}
                </option>
              ))}
            </select>
          </label>
          {areaLoadFailed ? (
            <span className="loss-map-block__notice">
              街镇空间数据不可用
            </span>
          ) : null}
          {selectedProductId && artifactLoadFailed ? (
            <span className="loss-map-block__notice">
              格网数据不可用
            </span>
          ) : null}
          {fusedIntensityProductId && fusedArtifactLoadFailed ? (
            <span className="loss-map-block__notice">
              融合烈度格网不可用
            </span>
          ) : null}
        </div>
        <LossMap
          center={DEFAULT_SHANGHAI_CENTER}
          tileUrlTemplate={tileUrlTemplate}
          townFeatures={townFeatures}
          gridArtifact={gridArtifact}
          fusedIntensityArtifact={fusedIntensityArtifact}
          selectedTownCode={selectedTownCode}
          selectedProductLabel={
            selectedProduct
              ? PRODUCT_LABELS[selectedProduct.product_type] ??
                selectedProduct.product_type
              : undefined
          }
          onTownSelect={setSelectedTownCode}
        />
      </div>

      {result.products.length === 0 ? (
        <p className="loss-assessment__empty">损失评估结果尚未发布</p>
      ) : (
        <div className="loss-product-list">
          {result.products.map((product) => (
            <LossProductCard key={product.product_id} product={product} />
          ))}
        </div>
      )}
    </section>
  );
}

function LossProductCard({ product }: { product: LossProductSummary }) {
  const statusLabel = productStatusLabel(product);
  const statusClass = productStatusClass(product);
  const metrics = buildMetricRows(product.metrics);
  const reasonVisible =
    product.reason !== null &&
    (product.status === "unavailable" ||
      product.status === "invalid" ||
      product.needs_review);

  return (
    <article className="loss-product" aria-labelledby={`loss-product-${product.product_id}-title`}>
      <header className="loss-product__header">
        <div>
          <p className="eyebrow">{product.product_id}</p>
          <h3 id={`loss-product-${product.product_id}-title`}>
            {PRODUCT_LABELS[product.product_type] ?? product.product_type}
          </h3>
        </div>
        <div className="loss-product__badges">
          <span className={`loss-product-status loss-product-status--${statusClass}`}>
            {statusLabel}
          </span>
          <span className="loss-product-quality">{product.quality_grade}</span>
          <span className="loss-product-calibration">
            {CALIBRATION_LABELS[product.calibration_status] ??
              product.calibration_status}
          </span>
        </div>
      </header>

      <dl className="loss-product-facts">
        <div>
          <dt>覆盖度</dt>
          <dd>{formatCoverage(product.coverage_ratio)}</dd>
        </div>
        <div>
          <dt>算法版本</dt>
          <dd className="mono">{product.algorithm_version}</dd>
        </div>
        <div>
          <dt>参数版本</dt>
          <dd className="mono">{product.parameter_version}</dd>
        </div>
        <div>
          <dt>区域配置</dt>
          <dd className="mono">{product.region_profile_version}</dd>
        </div>
        {product.partial_scope ? (
          <div>
            <dt>覆盖范围</dt>
            <dd>部分范围</dd>
          </div>
        ) : null}
        {product.spatialized_estimate ? (
          <div>
            <dt>输出形式</dt>
            <dd>空间化估算</dd>
          </div>
        ) : null}
      </dl>

      {reasonVisible ? (
        <div className="loss-product-reason">
          <span className="loss-product-reason__label">原因</span>
          <span className="loss-product-reason__text">{product.reason}</span>
        </div>
      ) : null}

      {metrics.length > 0 ? (
        <div className="loss-metrics">
          <div className="loss-metrics__heading">损失指标</div>
          <div className="loss-metrics__scroll">
            <table className="loss-metrics__table">
              <thead>
                <tr>
                  <th scope="col">区域</th>
                  <th scope="col">指标</th>
                  <th scope="col">低值</th>
                  <th scope="col">中值</th>
                  <th scope="col">高值</th>
                  <th scope="col">单位</th>
                </tr>
              </thead>
              <tbody>
                {metrics.map((metric) => (
                  <MetricRowView key={metric.id} metric={metric} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <p className="loss-metrics__empty">暂无指标数据</p>
      )}
    </article>
  );
}

function MetricRowView({ metric }: { metric: MetricRow }) {
  return (
    <tr>
      <td>{formatArea(metric)}</td>
      <td>{formatMetricKey(metric.metricKey)}</td>
      <td>
        {metric.values.low ? (
          <MetricValue value={metric.values.low} />
        ) : (
          "-"
        )}
      </td>
      <td>
        {metric.values.central ? (
          <MetricValue value={metric.values.central} />
        ) : (
          "-"
        )}
      </td>
      <td>
        {metric.values.high ? (
          <MetricValue value={metric.values.high} />
        ) : (
          "-"
        )}
      </td>
      <td>{metric.unit}</td>
    </tr>
  );
}

function MetricValue({ value }: { value: LossValue }) {
  return <span className={`loss-value loss-value--${value.value_status}`}>{formatLossValue(value)}</span>;
}

function buildMetricRows(metrics: LossValue[]): MetricRow[] {
  const rows = new Map<string, MetricRow>();

  for (const metric of metrics) {
    const id = `${metric.area_scope}:${metric.area_code}:${metric.metric_key}`;
    const existing = rows.get(id);
    if (existing) {
      existing.values[metric.value_type] = metric;
      continue;
    }

    rows.set(id, {
      id,
      areaScope: metric.area_scope,
      areaCode: metric.area_code,
      areaName: metric.area_name,
      metricKey: metric.metric_key,
      unit: metric.unit,
      precision: metric.precision,
      values: {
        [metric.value_type]: metric,
      },
    });
  }

  return [...rows.values()];
}

function productStatusLabel(product: LossProductSummary): string {
  if (
    product.status === "unavailable" ||
    product.status === "invalid" ||
    product.quality_grade === "L0"
  ) {
    return "不可计算";
  }
  if (product.quality_grade === "L3" || product.needs_review) {
    return "待复核";
  }
  if (product.status === "partial") {
    return "部分可用";
  }
  return "值班结果";
}

function productStatusClass(product: LossProductSummary): string {
  if (
    product.status === "unavailable" ||
    product.status === "invalid" ||
    product.quality_grade === "L0"
  ) {
    return "unavailable";
  }
  if (product.quality_grade === "L3" || product.needs_review) {
    return "review";
  }
  if (product.status === "partial") {
    return "partial";
  }
  return "complete";
}

function formatCoverage(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function formatArea(metric: MetricRow): string {
  if (metric.areaName) {
    return metric.areaName;
  }
  if (metric.areaScope === "city") {
    return "全市";
  }
  return metric.areaCode;
}

function formatMetricKey(metricKey: string): string {
  if (metricKey.endsWith(".quantity")) {
    const resourceKey = metricKey.slice(0, -".quantity".length);
    return `${RESOURCE_LABELS[resourceKey] ?? resourceKey}数量`;
  }
  return METRIC_LABELS[metricKey] ?? metricKey;
}

function formatLossValue(value: LossValue): string {
  if (value.value_status === "unavailable") {
    return "不可用";
  }
  if (value.value_status === "not_applicable") {
    return "不适用";
  }
  if (value.value_status === "rounded_to_zero") {
    return `${formatNumeric(value, 0)}（舍入）`;
  }
  return formatNumeric(value, value.precision);
}

function formatNumeric(value: LossValue, precision: number | null): string {
  if (value.numeric_value === null || !Number.isFinite(value.numeric_value)) {
    return "不可用";
  }
  const digits =
    precision === null || precision < 0 ? 0 : Math.min(precision, 6);
  return value.numeric_value.toFixed(digits);
}
