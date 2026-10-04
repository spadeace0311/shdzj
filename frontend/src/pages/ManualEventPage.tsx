import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { createManualEvent, manualEventErrorMessage } from "../api/client";

const EMPTY_FORM = {
  origin_time: "",
  longitude: "",
  latitude: "",
  magnitude: "",
  depth_km: "",
  source: "",
  source_event_id: "",
  place: "",
  event_kind: "manual",
};

export function ManualEventPage() {
  const [form, setForm] = useState(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState<{ kind: "success" | "error"; text: string } | null>(
    null,
  );

  function updateField(field: keyof typeof EMPTY_FORM, value: string) {
    setForm((current) => ({ ...current, [field]: value }));
  }

  function toIsoWithTimezone(value: string): string {
    if (value.length === 16) {
      return `${value}:00+08:00`;
    }
    return value;
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const required: Array<keyof typeof EMPTY_FORM> = [
      "origin_time",
      "longitude",
      "latitude",
      "magnitude",
      "depth_km",
      "source",
    ];
    if (required.some((field) => !form[field].trim())) {
      setMessage({ kind: "error", text: "请完整填写 6 个必填字段" });
      return;
    }

    setMessage(null);
    setSubmitting(true);
    try {
      await createManualEvent({
        origin_time: toIsoWithTimezone(form.origin_time),
        longitude: form.longitude,
        latitude: form.latitude,
        magnitude: form.magnitude,
        depth_km: form.depth_km,
        source: form.source.trim(),
        ...(form.source_event_id.trim()
          ? { source_event_id: form.source_event_id.trim() }
          : {}),
        ...(form.place.trim() ? { place: form.place.trim() } : {}),
        event_kind: form.event_kind,
      });
      setMessage({ kind: "success", text: "人工地震事件已提交并启动评估" });
    } catch (error) {
      setMessage({ kind: "error", text: manualEventErrorMessage(error) });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="page-section page-section--narrow" aria-labelledby="manual-title">
      <header className="page-heading">
        <div>
          <p className="eyebrow">人工触发</p>
          <h1 id="manual-title">创建人工地震事件</h1>
        </div>
        <Link className="text-button" to="/">
          返回事件列表
        </Link>
      </header>

      <form className="manual-form" onSubmit={handleSubmit}>
        <fieldset>
          <legend>必填信息</legend>
          <div className="form-grid">
            <label htmlFor="origin_time">发震时刻</label>
            <input
              id="origin_time"
              type="datetime-local"
              value={form.origin_time}
              onChange={(event) => updateField("origin_time", event.target.value)}
              required
            />

            <label htmlFor="longitude">经度</label>
            <input
              id="longitude"
              type="number"
              step="0.000001"
              min="-180"
              max="180"
              value={form.longitude}
              onChange={(event) => updateField("longitude", event.target.value)}
              required
            />

            <label htmlFor="latitude">纬度</label>
            <input
              id="latitude"
              type="number"
              step="0.000001"
              min="-90"
              max="90"
              value={form.latitude}
              onChange={(event) => updateField("latitude", event.target.value)}
              required
            />

            <label htmlFor="magnitude">震级</label>
            <input
              id="magnitude"
              type="number"
              step="0.1"
              min="0"
              max="10"
              value={form.magnitude}
              onChange={(event) => updateField("magnitude", event.target.value)}
              required
            />

            <label htmlFor="depth_km">震源深度</label>
            <input
              id="depth_km"
              type="number"
              step="0.01"
              min="0"
              max="1000"
              value={form.depth_km}
              onChange={(event) => updateField("depth_km", event.target.value)}
              required
            />

            <label htmlFor="source">数据来源</label>
            <input
              id="source"
              type="text"
              maxLength={32}
              value={form.source}
              onChange={(event) => updateField("source", event.target.value)}
              required
            />

            <label htmlFor="event_kind">事件类型</label>
            <select
              id="event_kind"
              value={form.event_kind}
              onChange={(event) => updateField("event_kind", event.target.value)}
            >
              <option value="manual">人工事件</option>
              <option value="test">测试</option>
              <option value="drill">演练</option>
            </select>
          </div>
        </fieldset>

        <fieldset>
          <legend>可选信息</legend>
          <div className="form-grid">
            <label htmlFor="source_event_id">来源事件编号</label>
            <input
              id="source_event_id"
              type="text"
              maxLength={128}
              value={form.source_event_id}
              onChange={(event) => updateField("source_event_id", event.target.value)}
            />

            <label htmlFor="place">地点</label>
            <input
              id="place"
              type="text"
              maxLength={256}
              value={form.place}
              onChange={(event) => updateField("place", event.target.value)}
            />
          </div>
        </fieldset>

        <div className="form-actions">
          {message ? (
            <p
              className={message.kind === "success" ? "form-success" : "form-error"}
              role="status"
            >
              {message.text}
            </p>
          ) : null}
          <button className="primary-button" type="submit" disabled={submitting}>
            {submitting ? "提交中" : "启动评估"}
          </button>
        </div>
      </form>
    </section>
  );
}
