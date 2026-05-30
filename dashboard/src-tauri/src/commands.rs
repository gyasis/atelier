use crate::fetcher;
use crate::log_watcher;

#[tauri::command]
pub async fn get_pressure() -> Result<serde_json::Value, String> {
    let p = fetcher::fetch_pressure().await;
    serde_json::to_value(p).map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn get_telemetry() -> Result<serde_json::Value, String> {
    let t = fetcher::fetch_telemetry().await;
    serde_json::to_value(t).map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn get_predictor_stats() -> Result<serde_json::Value, String> {
    let s = fetcher::fetch_predictor_stats().await;
    serde_json::to_value(s).map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn get_sidecars() -> Result<serde_json::Value, String> {
    // Fetch all sidecar /readyz endpoints + Ollama /api/ps concurrently
    let (omnivoice, kokoro, dia, comfyui, ollama) = tokio::join!(
        fetcher::fetch_sidecar(8770),
        fetcher::fetch_sidecar(8765),
        fetcher::fetch_sidecar(8769),
        fetcher::fetch_sidecar(8188),
        fetcher::fetch_ollama_ps(),
    );
    Ok(serde_json::json!({
        "omnivoice": omnivoice,
        "kokoro":    kokoro,
        "dia":       dia,
        "comfyui":   comfyui,
        "ollama":    ollama,
    }))
}

#[tauri::command]
pub async fn get_pressure_summary() -> String {
    let p = fetcher::fetch_pressure().await;
    format!("{:.1} GB · {}", p.resident_gb, p.level)
}

/// Returns the last N log-derived metrics (tokens/sec, synth RTF, img progress).
#[tauri::command]
pub async fn get_log_metrics() -> Result<serde_json::Value, String> {
    let metrics = log_watcher::drain_metrics();
    serde_json::to_value(metrics).map_err(|e| e.to_string())
}
