import { useEffect, useState } from "react";
import { fetchHealth, fetchModels, switchModel } from "../api";
import { isApiError, type ModelsResponse, type SwitchModelRequest } from "../types";

interface Props {
  switching: boolean;
  onSwitchStart: () => void;
  onSwitchDone: () => void;
}

export function ModelPanel({ switching, onSwitchStart, onSwitchDone }: Props) {
  const [data, setData] = useState<ModelsResponse | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [showTuning, setShowTuning] = useState(false);
  const [alignerBackend, setAlignerBackend] = useState<string>("");

  // Tuning fields (initialized from backend defaults on load)
  const [ctxSize, setCtxSize] = useState<number>(16384);
  const [kvQuant, setKvQuant] = useState<string>("q4_0");
  const [ngl, setNgl] = useState<number>(99);
  const [threads, setThreads] = useState<string>("");

  useEffect(() => {
    fetchModels()
      .then((d) => {
        setData(d);
        setSelected(d.current_model ?? d.models[0]?.name ?? "");
        if (d.tuning) {
          setCtxSize(d.tuning.ctx_size);
          setKvQuant(d.tuning.kv_quant);
          setNgl(d.tuning.n_gpu_layers);
          if (d.tuning.threads != null) setThreads(String(d.tuning.threads));
        }
      })
      .catch(() => {});
    fetchHealth()
      .then((h) => setAlignerBackend(h.aligner_backend))
      .catch(() => {});
  }, []);

  // Refresh model list after a successful switch
  useEffect(() => {
    if (!switching && data) {
      fetchModels().then(setData).catch(() => {});
    }
  }, [switching]); // eslint-disable-line react-hooks/exhaustive-deps

  const models = data?.models ?? [];
  const currentName = data?.current_model ?? "";

  const handleSwitch = async () => {
    if (!selected || switching) return;
    onSwitchStart();
    const req: SwitchModelRequest = {
      model: selected,
      ctx_size: ctxSize,
      kv_quant: kvQuant,
      n_gpu_layers: ngl,
      threads: threads ? parseInt(threads, 10) : undefined,
    };
    const res = await switchModel(req);
    if (isApiError(res)) {
      // Error is surfaced via boot_state; the overlay will show it
    }
    onSwitchDone();
  };

  const disabled = switching || models.length === 0;
  const isCurrent = selected === currentName;

  return (
    <div className="border border-neutral-800 p-6">
      <h2 className="font-mono text-xs uppercase tracking-widest text-neutral-500 mb-4">
        Model
      </h2>

      {/* Current model */}
      <p className="font-mono text-xs text-neutral-500 mb-1">Current</p>
      <p className="font-mono text-sm font-bold text-neutral-100 break-all">
        {currentName || "—"}
      </p>
      {alignerBackend && (
        <p className="font-mono text-[10px] text-neutral-600 mt-1 mb-4">
          Aligner:{" "}
          <span className={alignerBackend === "cpu" ? "text-neutral-500" : "text-amber-600"}>
            {alignerBackend === "gpu" ? "GPU (qwen-asr)" : "CPU (CrispASR)"}
          </span>
        </p>
      )}
      {!alignerBackend && <div className="mb-4" />}

      {/* Model selector */}
      <label className="font-mono text-xs text-neutral-500 mb-1 block">
        Switch to
      </label>
      <select
        value={selected}
        onChange={(e) => setSelected(e.target.value)}
        disabled={disabled}
        className="w-full bg-black border border-neutral-700 text-neutral-200 font-mono text-sm px-3 py-2 disabled:opacity-40 disabled:cursor-not-allowed focus:border-white outline-none"
      >
        {models.map((m) => (
          <option key={m.name} value={m.name}>
            {m.name} ({(m.size / 1073741824).toFixed(1)} GB)
          </option>
        ))}
      </select>

      {/* Tuning toggle */}
      <button
        onClick={() => setShowTuning(!showTuning)}
        disabled={disabled}
        className="mt-3 font-mono text-[10px] uppercase tracking-widest text-neutral-600 hover:text-neutral-400 disabled:opacity-40 transition-colors"
      >
        {showTuning ? "▲ Hide tuning" : "▼ Show tuning"}
      </button>

      {showTuning && (
        <div className="mt-3 space-y-3 animate-fade-in">
          {/* n_gpu_layers */}
          <div>
            <label className="font-mono text-xs text-neutral-500 flex justify-between">
              <span>GPU layers</span>
              <span className="text-neutral-400">{ngl}</span>
            </label>
            <input
              type="range"
              min={0}
              max={99}
              value={ngl}
              onChange={(e) => setNgl(parseInt(e.target.value, 10))}
              disabled={disabled}
              className="w-full accent-white disabled:opacity-40"
            />
          </div>

          {/* ctx_size */}
          <div>
            <label className="font-mono text-xs text-neutral-500 mb-1 block">
              Context size
            </label>
            <select
              value={ctxSize}
              onChange={(e) => setCtxSize(parseInt(e.target.value, 10))}
              disabled={disabled}
              className="w-full bg-black border border-neutral-700 text-neutral-200 font-mono text-xs px-3 py-1.5 disabled:opacity-40 outline-none"
            >
              {[4096, 8192, 16384, 32768].map((v) => (
                <option key={v} value={v}>
                  {v.toLocaleString()}
                </option>
              ))}
            </select>
          </div>

          {/* kv_quant */}
          <div>
            <label className="font-mono text-xs text-neutral-500 mb-1 block">
              KV cache quant
            </label>
            <select
              value={kvQuant}
              onChange={(e) => setKvQuant(e.target.value)}
              disabled={disabled}
              className="w-full bg-black border border-neutral-700 text-neutral-200 font-mono text-xs px-3 py-1.5 disabled:opacity-40 outline-none"
            >
              {["q4_0", "q8_0", "f16"].map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
          </div>

          {/* threads */}
          <div>
            <label className="font-mono text-xs text-neutral-500 mb-1 block">
              Threads (blank = auto)
            </label>
            <input
              type="number"
              min={1}
              max={128}
              value={threads}
              onChange={(e) => setThreads(e.target.value)}
              disabled={disabled}
              placeholder="auto"
              className="w-full bg-black border border-neutral-700 text-neutral-200 font-mono text-xs px-3 py-1.5 disabled:opacity-40 outline-none placeholder:text-neutral-700"
            />
          </div>
        </div>
      )}

      {/* Switch button */}
      <button
        onClick={handleSwitch}
        disabled={disabled || isCurrent}
        className="w-full mt-4 py-2.5 font-mono text-xs font-bold uppercase tracking-widest bg-white text-black disabled:bg-neutral-800 disabled:text-neutral-600 disabled:cursor-not-allowed hover:bg-neutral-200 transition-colors"
      >
        {switching ? "Switching…" : isCurrent ? "Current" : "Switch Model"}
      </button>
    </div>
  );
}
