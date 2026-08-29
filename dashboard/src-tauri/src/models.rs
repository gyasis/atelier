use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct TenantEntry {
    #[serde(default)]
    pub tenant: String,   // "ollama" | "atelier"
    pub name: String,
    #[serde(default = "default_state")]
    pub state: String,
    #[serde(default)]
    pub active_jobs: u32,
    #[serde(default)]
    pub queue_depth: u32,
    // NOTE: was `f64` (non-Option). The governor sends explicit `"mem_gb": null` for
    // unmeasured cold sidecars (mlxlm/radiogen/maisi) — serde cannot deserialize JSON
    // `null` into a bare `f64` even with `#[serde(default)]` (that attribute only
    // covers a MISSING key, not a present-but-null one). That type mismatch failed
    // the deserialize of the *entire* PressureResponse, which fetcher::fetch_pressure()
    // silently swallowed via `.unwrap_or_default()` — collapsing the whole response
    // (tenants, resident_gb, free_gb, everything) to zeroed defaults, which is why the
    // "Loaded Now" donut rendered a single blank 0-value wedge. `Option<f64>` fixes it.
    #[serde(default)]
    pub mem_gb: Option<f64>,
    #[serde(default)]
    pub keep_warm: Option<bool>,
    #[serde(default)]
    pub active_elapsed_s: Option<f64>,
    #[serde(default)]
    pub device: Option<String>,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub available_models: Option<Vec<String>>,
    #[serde(default)]
    pub cold_rss_gb: Option<f64>,
    #[serde(default)]
    pub governed: Option<bool>,
}
fn default_state() -> String { "unknown".into() }

#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct AutohealConfig {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default)]
    pub floor_gb: f64,
    #[serde(default)]
    pub grace_s: f64,
}

#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct Constitution {
    #[serde(default)]
    pub laws: Vec<String>,
    #[serde(default)]
    pub autoheal: AutohealConfig,
    #[serde(default)]
    pub cliff_gb: f64,
    #[serde(default)]
    pub warn_gb: f64,
}

#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct PressureResponse {
    pub level: String,
    pub free_gb: f64,
    pub resident_gb: f64,
    #[serde(default)]
    pub swapouts: u64,
    #[serde(default)]
    pub tenants: Vec<TenantEntry>,
    #[serde(default)]
    pub auto_action: Option<serde_json::Value>,
    #[serde(default)]
    pub recommendation: Option<serde_json::Value>,
    #[serde(default)]
    pub committed_gb: Option<f64>,
    #[serde(default)]
    pub baseline_gb: Option<f64>,
    #[serde(default)]
    pub budget_gb: Option<f64>,
    // kept as loosely-typed JSON — these are display-only lists whose per-item shape
    // may still evolve; typing them strictly would reintroduce the same fragility.
    #[serde(default)]
    pub top_procs: Vec<serde_json::Value>,
    #[serde(default)]
    pub autoheal: Vec<serde_json::Value>,
    #[serde(default)]
    pub constitution: Option<Constitution>,
}

// A /telemetry `recent_calls` entry. A plain log line carries only the first five
// fields; a proxied call adds model/backend/via/prompt/tokens/etc. EVERYTHING here is
// optional with `#[serde(default)]` so a missing OR null field can never fail the whole
// deserialize (the same class of bug that blanked the donut). `status` + `latency` are
// `serde_json::Value` because the governor may send either a string ("200", "0.42s") or
// a number — a fixed scalar type would fail on the other shape. This struct MUST declare
// every field the frontend reads, because `get_telemetry` re-serializes THIS struct and
// silently drops any field it doesn't know (which is why recent-calls was empty before).
#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct RecentCall {
    #[serde(default)]
    pub at: Option<String>,
    #[serde(default)]
    pub ts: f64,
    #[serde(default)]
    pub status: Option<serde_json::Value>,
    #[serde(default)]
    pub latency: Option<serde_json::Value>,
    #[serde(default)]
    pub path: Option<String>,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub backend: Option<String>,
    #[serde(default)]
    pub via: Option<String>,
    #[serde(default)]
    pub prompt: Option<String>,
    #[serde(default)]
    pub in_tok: Option<u64>,
    #[serde(default)]
    pub eval_tokens: Option<u64>,
    #[serde(default)]
    pub tok_s: Option<f64>,
    #[serde(default)]
    pub ttft_ms: Option<f64>,
    #[serde(default)]
    pub num_ctx: Option<u64>,
    #[serde(default)]
    pub est_gb: Option<f64>,
}

// A /telemetry `recent_synths` entry — TTS ({engine, seconds, chars?, rtf?, num_step?})
// OR ASR ({engine, kind:"asr", model, audio_s, chars, seconds, rtf?}). All optional so
// either shape deserializes; `rtf`/`num_step` are often JSON null → Option handles it.
#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct RecentSynth {
    #[serde(default)]
    pub at: Option<String>,
    #[serde(default)]
    pub ts: f64,
    #[serde(default)]
    pub engine: Option<String>,
    #[serde(default)]
    pub seconds: Option<f64>,
    #[serde(default)]
    pub chars: Option<u64>,
    #[serde(default)]
    pub rtf: Option<f64>,
    #[serde(default)]
    pub num_step: Option<u64>,
    #[serde(default)]
    pub kind: Option<String>,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub audio_s: Option<f64>,
}

#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct TelemetryResponse {
    #[serde(default)]
    pub recent_calls: Vec<RecentCall>,
    // pass-through display lists / blobs whose per-item shape isn't pinned — keep them
    // as raw JSON so they survive the round-trip and never fail deserialize.
    #[serde(default)]
    pub recent_events: Vec<serde_json::Value>,
    #[serde(default)]
    pub recent_synths: Vec<RecentSynth>,
    #[serde(default)]
    pub last_spill: Option<serde_json::Value>,
    #[serde(default)]
    pub ollama_perf: Option<serde_json::Value>,
}

#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct ClassStat {
    pub kind: String,
    pub model: String,
    pub runs: u32,
    pub avg_rate: f64,
    pub avg_seconds: f64,
}

#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct PredictorStatsResponse {
    #[serde(default)]
    pub stats: Vec<ClassStat>,
}

#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct SidecarReadyz {
    #[serde(default)]
    pub lifecycle: String,
    #[serde(default)]
    pub active_jobs: u32,
    #[serde(default)]
    pub queue_depth: u32,
}

/// Parsed log metric emitted by the log-tail watcher.
#[derive(Debug, Serialize, Clone)]
pub struct LogMetric {
    pub source: String,          // "ollama" | "omnivoice" | "comfyui"
    pub metric: String,          // "tokens_per_sec" | "synth_rtf" | "img_progress_pct" | "model_call"
    pub value: f64,
    pub ts: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,   // model name for model_call (e.g. "gemma4:latest")
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,    // HTTP path for model_call (e.g. "/api/chat")
    #[serde(skip_serializing_if = "Option::is_none")]
    pub status: Option<u16>,     // HTTP status for model_call
}
