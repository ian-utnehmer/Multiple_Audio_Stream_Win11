import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AudioLines,
  Cable,
  CirclePower,
  Plus,
  RefreshCw,
  SlidersHorizontal,
  Trash2,
  Volume2,
} from "lucide-react";
import "./styles.css";

const API = {
  async getState() {
    return request("/api/state");
  },
  async start() {
    return request("/api/start", { method: "POST" });
  },
  async stop() {
    return request("/api/stop", { method: "POST" });
  },
  async refresh() {
    return request("/api/refresh", { method: "POST" });
  },
  async update(payload) {
    return request("/api/update", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  async addOutput() {
    return request("/api/outputs", { method: "POST" });
  },
  async removeOutput(id) {
    return request(`/api/outputs/${encodeURIComponent(id)}`, { method: "DELETE" });
  },
  async shutdown() {
    return request("/api/shutdown", { method: "POST" });
  },
};

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Request failed");
  }
  return payload;
}

function App() {
  const [state, setState] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const pollRef = useRef(null);

  const applyState = useCallback((nextState, options = {}) => {
    setState((previous) => {
      if (!previous || options.replaceControls) {
        return nextState;
      }
      if (previous.devices?.version === nextState.devices?.version) {
        return {
          ...previous,
          appTitle: nextState.appTitle,
          routing: nextState.routing,
          status: nextState.status,
          lastError: nextState.lastError,
        };
      }
      if (isControlActive()) {
        return {
          ...previous,
          appTitle: nextState.appTitle,
          routing: nextState.routing,
          status: nextState.status,
          lastError: nextState.lastError,
        };
      }
      return nextState;
    });
    setLoading(false);
    if (nextState.lastError) {
      setError("Audio routing stopped. Check audio_splitter_error.log for details.");
    }
  }, []);

  const run = useCallback(
    async (label, action) => {
      try {
        setBusy(label);
        setError("");
        applyState(await action(), { replaceControls: true });
      } catch (err) {
        setError(err.message || String(err));
      } finally {
        setBusy("");
      }
    },
    [applyState],
  );

  useEffect(() => {
    let alive = true;
    API.getState()
      .then((payload) => {
        if (alive) applyState(payload, { replaceControls: true });
      })
      .catch((err) => {
        if (alive) {
          setError(err.message || String(err));
          setLoading(false);
        }
      });

    pollRef.current = window.setInterval(() => {
      API.getState()
        .then((payload) => {
          if (alive) applyState(payload);
        })
        .catch((err) => {
          if (alive) setError(err.message || String(err));
        });
    }, 450);

    return () => {
      alive = false;
      window.clearInterval(pollRef.current);
    };
  }, [applyState]);

  const sourceOptions = state?.devices.sources ?? [];
  const outputOptions = state?.devices.outputs ?? [];
  const rows = state?.selection.outputs ?? [];
  const running = Boolean(state?.routing.running);

  const updateOptimistic = useCallback(
    (patch) => {
      if (!state) return;
      const next = mergeState(state, patch);
      setState(next);
      API.update(patch)
        .then((payload) => applyState(payload))
        .catch((err) => setError(err.message || String(err)));
    },
    [applyState, state],
  );

  const meterStyle = useMemo(
    () => ({ width: `${Math.round((state?.routing.level ?? 0) * 100)}%` }),
    [state?.routing.level],
  );

  if (loading) {
    return (
      <main className="loading-shell">
        <div className="loading-panel">
          <AudioLines size={34} />
          <h1>Launching Audio Splitter</h1>
          <p>Connecting to the local audio host...</p>
        </div>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            <AudioLines size={24} />
          </div>
          <div>
            <h1>Audio Splitter</h1>
            <p>Live multi-output routing</p>
          </div>
        </div>

        <button
          className={running ? "primary danger" : "primary"}
          disabled={Boolean(busy)}
          onClick={() => run(running ? "Stopping" : "Starting", running ? API.stop : API.start)}
        >
          <CirclePower size={18} />
          {running ? "Stop Routing" : "Start Routing"}
        </button>

        <button className="secondary" disabled={Boolean(busy)} onClick={() => run("Refreshing", API.refresh)}>
          <RefreshCw size={17} />
          Refresh Devices
        </button>

        <div className="status-card">
          <div className="status-heading">
            <Activity size={16} />
            <span>Signal</span>
          </div>
          <div className="meter">
            <div style={meterStyle} />
          </div>
          <p>{state?.status}</p>
          {state?.routing.queueBlocks > 0 && <span>Live queue: {state.routing.queueBlocks} block</span>}
          {state?.routing.resyncBlocks > 0 && <span>{state.routing.resyncBlocks} resync block(s)</span>}
        </div>

        <button className="quiet" onClick={() => run("Shutting down", API.shutdown)}>
          Quit App
        </button>
      </aside>

      <section className="workspace">
        <header className="page-header">
          <div>
            <h2>Routing Console</h2>
            <p>Choose a source, add any number of outputs, and tune every stream live.</p>
          </div>
          <div className={running ? "pill live" : "pill"}>
            <span />
            {running ? "Live" : "Idle"}
          </div>
        </header>

        {error && <div className="error-bar">{error}</div>}

        <section className="panel">
          <PanelTitle icon={<Cable size={18} />} title="Capture" subtitle="The loopback stream Audio Splitter mirrors." />
          <label className="field">
            <span>Loopback source</span>
            <select
              value={state.selection.sourceKey}
              onChange={(event) => updateOptimistic({ sourceKey: event.target.value })}
            >
              {sourceOptions.length === 0 && <option value="">No loopback sources found</option>}
              {sourceOptions.map((device) => (
                <option value={device.key} key={device.key}>
                  {device.label}
                </option>
              ))}
            </select>
          </label>
        </section>

        <section className="panel outputs-panel">
          <div className="panel-toolbar">
            <PanelTitle icon={<Volume2 size={18} />} title="Additional Outputs" subtitle="Each row routes to one playback device." />
            <button className="add-button" disabled={Boolean(busy)} onClick={() => run("Adding output", API.addOutput)}>
              <Plus size={17} />
              Add Output
            </button>
          </div>

          <div className="output-list">
            {rows.map((row, index) => (
              <OutputRow
                key={row.id}
                index={index}
                row={row}
                outputOptions={outputOptions}
                disableRemove={rows.length <= 1}
                onChange={(nextRow) =>
                  updateOptimistic({
                    outputs: rows.map((candidate) => (candidate.id === row.id ? nextRow : candidate)),
                  })
                }
                onRemove={() => run("Removing output", () => API.removeOutput(row.id))}
              />
            ))}
          </div>
        </section>

        <section className="panel">
          <PanelTitle icon={<SlidersHorizontal size={18} />} title="Routing Settings" subtitle="Latency, volume, and feedback controls." />

          <label className="field">
            <span>Main output volume</span>
            <SliderRow
              min={0}
              max={100}
              value={state.settings.masterVolume}
              onChange={(value) => updateOptimistic({ masterVolume: value })}
            />
          </label>

          <div className="settings-grid">
            <SegmentGroup
              label="Sample rate"
              value={String(state.settings.sampleRate)}
              options={["44100", "48000"]}
              onChange={(value) => updateOptimistic({ sampleRate: Number(value) })}
            />
            <SegmentGroup
              label="Block size"
              value={String(state.settings.blockSize)}
              options={["64", "128", "256", "512", "1024", "2048", "4096"]}
              onChange={(value) => updateOptimistic({ blockSize: Number(value) })}
            />
          </div>

          <label className="toggle-row">
            <input
              type="checkbox"
              checked={Boolean(state.settings.allowFeedback)}
              onChange={(event) => updateOptimistic({ allowFeedback: event.target.checked })}
            />
            <span>Allow output back into the captured source device</span>
          </label>
        </section>
      </section>
    </main>
  );
}

function PanelTitle({ icon, title, subtitle }) {
  return (
    <div className="panel-title">
      <div>{icon}</div>
      <span>
        <strong>{title}</strong>
        <small>{subtitle}</small>
      </span>
    </div>
  );
}

function OutputRow({ index, row, outputOptions, disableRemove, onChange, onRemove }) {
  return (
    <div className="output-row">
      <div className="output-index">Output {index + 1}</div>
      <select value={row.deviceKey} onChange={(event) => onChange({ ...row, deviceKey: event.target.value })}>
        {outputOptions.map((device) => (
          <option value={device.key} key={device.key}>
            {device.label}
          </option>
        ))}
      </select>
      <SliderRow min={0} max={500} value={row.volume} onChange={(volume) => onChange({ ...row, volume })} />
      <button className="icon-button" disabled={disableRemove} onClick={onRemove} title="Remove output">
        <Trash2 size={17} />
      </button>
    </div>
  );
}

function SliderRow({ min, max, value, onChange }) {
  return (
    <div className="slider-row">
      <input
        type="range"
        min={min}
        max={max}
        value={Math.round(value)}
        onChange={(event) => onChange(Number(event.target.value))}
      />
      <output>{Math.round(value)}%</output>
    </div>
  );
}

function SegmentGroup({ label, value, options, onChange }) {
  return (
    <fieldset className="segment-group">
      <legend>{label}</legend>
      <div>
        {options.map((option) => (
          <button
            key={option}
            type="button"
            className={value === option ? "selected" : ""}
            onClick={() => onChange(option)}
          >
            {option}
          </button>
        ))}
      </div>
    </fieldset>
  );
}

function mergeState(state, patch) {
  const next = structuredClone(state);
  if ("sourceKey" in patch) next.selection.sourceKey = patch.sourceKey;
  if ("masterVolume" in patch) next.settings.masterVolume = patch.masterVolume;
  if ("sampleRate" in patch) next.settings.sampleRate = patch.sampleRate;
  if ("blockSize" in patch) next.settings.blockSize = patch.blockSize;
  if ("allowFeedback" in patch) next.settings.allowFeedback = patch.allowFeedback;
  if ("outputs" in patch) next.selection.outputs = patch.outputs;
  return next;
}

function isControlActive() {
  const activeElement = document.activeElement;
  if (!activeElement || !activeElement.closest) return false;
  return Boolean(activeElement.closest("select, input, .segment-group"));
}

createRoot(document.getElementById("root")).render(<App />);
