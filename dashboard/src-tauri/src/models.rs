use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct TenantEntry {
    pub name: String,
    pub state: String,
    pub active_jobs: u32,
    pub queue_depth: u32,
    pub mem_gb: f64,
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
}

#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct RecentCall {
    #[serde(default)]
    pub engine: String,
    #[serde(default)]
    pub chars: u32,
    #[serde(default)]
    pub output_tokens: u32,
    #[serde(default)]
    pub latency_s: f64,
    #[serde(default)]
    pub ts: f64,
}

#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct RecentSynth {
    #[serde(default)]
    pub chars: u32,
    #[serde(default)]
    pub seconds: f64,
    #[serde(default)]
    pub engine: String,
    #[serde(default)]
    pub ts: f64,
}

#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct TelemetryResponse {
    #[serde(default)]
    pub recent_calls: Vec<RecentCall>,
    #[serde(default)]
    pub recent_synths: Vec<RecentSynth>,
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
    pub source: String,   // "ollama" | "omnivoice" | "comfyui"
    pub metric: String,   // "tokens_per_sec" | "synth_rtf" | "img_progress_pct"
    pub value: f64,
    pub ts: u64,
}
