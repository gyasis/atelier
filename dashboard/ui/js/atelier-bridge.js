/**
 * Tauri bridge — wraps invoke() calls for each governor/sidecar endpoint.
 * Falls back to mock data when window.__TAURI__ is undefined (browser preview).
 */

const isTauri = typeof window !== 'undefined' && window.__TAURI__ !== undefined;

async function invoke(cmd, args = {}) {
  if (isTauri) {
    return window.__TAURI__.invoke(cmd, args);
  }
  return null;
}

export async function fetchPressure() {
  const data = await invoke('get_pressure');
  if (data) return data;
  return {
    level: 'warn', free_gb: 25.6, resident_gb: 38.4, swapouts: 0,
    tenants: [
      { name: 'omnivoice', state: 'busy', active_jobs: 3, queue_depth: 7, mem_gb: 18.0 },
      { name: 'kokoro',    state: 'idle', active_jobs: 0, queue_depth: 0, mem_gb: 4.0  },
      { name: 'comfyui',   state: 'busy', active_jobs: 1, queue_depth: 2, mem_gb: 12.0 },
      { name: 'qwen3:32b', state: 'idle', active_jobs: 0, queue_depth: 1, mem_gb: 20.0 },
    ],
    auto_action: null, recommendation: null,
  };
}

export async function fetchTelemetry() {
  const data = await invoke('get_telemetry');
  if (data) return data;
  return {
    recent_calls: [
      { engine: 'qwen3:32b', chars: 312, output_tokens: 187, latency_s: 1.8, ts: Date.now() - 2000 },
      { engine: 'qwen3:32b', chars: 890, output_tokens: 420, latency_s: 4.2, ts: Date.now() - 8000 },
    ],
    recent_synths: [
      { chars: 840, seconds: 3.2, engine: 'omnivoice', ts: Date.now() - 1000 },
      { chars: 620, seconds: 2.9, engine: 'omnivoice', ts: Date.now() - 5000 },
    ],
  };
}

export async function fetchPredictorStats() {
  const data = await invoke('get_predictor_stats');
  if (data) return data;
  return {
    stats: [
      { kind: 'llm', model: 'qwen3:32b',  runs: 142, avg_rate: 47.2, avg_seconds: 3.1 },
      { kind: 'tts', model: 'omnivoice',  runs: 89,  avg_rate: 0.31, avg_seconds: 3.0 },
      { kind: 'tts', model: 'kokoro',     runs: 34,  avg_rate: 0.18, avg_seconds: 2.2 },
      { kind: 'img', model: 'wan2.1-1.3b',runs: 12,  avg_rate: 0.08, avg_seconds: 11.4 },
    ],
  };
}

export async function fetchSidecars() {
  const data = await invoke('get_sidecars');
  if (data) return data;
  return {
    omnivoice: { lifecycle: 'running', active_jobs: 3, queue_depth: 7 },
    kokoro:    { lifecycle: 'running', active_jobs: 0, queue_depth: 0 },
    dia:       { lifecycle: 'stopped', active_jobs: 0, queue_depth: 0 },
    comfyui:   { lifecycle: 'running', active_jobs: 1, queue_depth: 2 },
    ollama:    { models: [{ name: 'qwen3:32b', size: 20000000000 }] },
  };
}
