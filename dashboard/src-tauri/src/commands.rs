use crate::fetcher;
use crate::log_watcher;
use tauri::Manager;

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
    // NOTE: Ollama models come from the governor's /pressure tenants now — the
    // dashboard no longer polls /api/ps here (it flooded ~/.ollama/logs/server.log
    // with GET lines, pushing real /api/chat|/api/generate calls out of the window).
    let (omnivoice, kokoro, whisper, dia, llamacpp, fastmlx, mlxlm, comfyui) = tokio::join!(
        fetcher::fetch_sidecar(8770),
        fetcher::fetch_sidecar(8765),
        fetcher::fetch_sidecar(8766),
        fetcher::fetch_sidecar(8769),
        fetcher::fetch_sidecar(8771),
        fetcher::fetch_sidecar(8772),
        fetcher::fetch_sidecar(8773),
        fetcher::fetch_sidecar(8188),
    );
    Ok(serde_json::json!({
        "omnivoice": omnivoice,
        "kokoro":    kokoro,
        "whisper":   whisper,
        "dia":       dia,
        "llamacpp":  llamacpp,
        "fastmlx":   fastmlx,
        "mlxlm":     mlxlm,
        "comfyui":   comfyui,
    }))
}

#[tauri::command]
pub async fn get_pressure_summary() -> String {
    let p = fetcher::fetch_pressure().await;
    format!("{:.1} GB · {}", p.resident_gb, p.level)
}

/// Returns the last N log-derived metrics (tokens/sec, synth RTF, img progress, model_call).
#[tauri::command]
pub async fn get_log_metrics() -> Result<serde_json::Value, String> {
    let metrics = log_watcher::drain_metrics();
    serde_json::to_value(metrics).map_err(|e| e.to_string())
}

/// Top 10 processes by resident memory (RSS) on this machine.
#[tauri::command]
pub async fn get_top_processes() -> Result<serde_json::Value, String> {
    let out = tokio::process::Command::new("ps")
        .args(["-eo", "pid,rss,comm", "-r"])
        .output()
        .await
        .map_err(|e| e.to_string())?;
    let stdout = String::from_utf8_lossy(&out.stdout);
    let mut rows = vec![];
    for line in stdout.lines().skip(1).take(10) {
        let parts: Vec<&str> = line.trim().splitn(3, ' ').collect();
        if parts.len() == 3 {
            let pid: u32 = parts[0].trim().parse().unwrap_or(0);
            let rss_kb: u64 = parts[1].trim().parse().unwrap_or(0);
            let name = parts[2].trim().split('/').last().unwrap_or(parts[2].trim());
            rows.push(serde_json::json!({
                "pid": pid,
                "rss_mb": (rss_kb as f64 / 1024.0).round(),
                "name": name,
            }));
        }
    }
    Ok(serde_json::json!({ "processes": rows }))
}

/// Where the dashboard panel layout is persisted (survives app quit, unlike
/// WKWebView localStorage which can live in an ephemeral store).
fn layout_path() -> std::path::PathBuf {
    let home = dirs_next::home_dir().unwrap_or_default();
    home.join(".atelier").join("dashboard-layout.json")
}

/// Persist the panel layout (JSON string) to disk.
#[tauri::command]
pub async fn save_layout(data: String) -> Result<(), String> {
    let p = layout_path();
    if let Some(dir) = p.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    std::fs::write(&p, data).map_err(|e| e.to_string())
}

/// Load the persisted panel layout (JSON string), or None if not saved yet.
#[tauri::command]
pub async fn load_layout() -> Result<Option<String>, String> {
    match std::fs::read_to_string(layout_path()) {
        Ok(s) => Ok(Some(s)),
        Err(_) => Ok(None),
    }
}

/// Resize the main window down to HUD card size.
#[tauri::command]
pub async fn resize_to_hud(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(w) = app.get_webview_window("main") {
        w.set_size(tauri::Size::Physical(tauri::PhysicalSize { width: 460, height: 380 }))
            .map_err(|e| e.to_string())?;
        w.set_resizable(false).map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Resize the window back to full dashboard size.
#[tauri::command]
pub async fn resize_to_main(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(w) = app.get_webview_window("main") {
        w.set_resizable(true).map_err(|e| e.to_string())?;
        w.set_size(tauri::Size::Physical(tauri::PhysicalSize { width: 1440, height: 820 }))
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Toggle the Mini HUD window (kept for tray menu compatibility).
#[tauri::command]
pub async fn toggle_hud_window(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(w) = app.get_webview_window("hud") {
        if w.is_visible().unwrap_or(false) {
            w.hide().map_err(|e| e.to_string())?;
        } else {
            w.show().map_err(|e| e.to_string())?;
            w.set_focus().map_err(|e| e.to_string())?;
        }
    } else {
        tauri::WebviewWindowBuilder::new(&app, "hud", tauri::WebviewUrl::App("hud.html".into()))
            .title("Atelier Mini HUD")
            .inner_size(420.0, 360.0)
            .resizable(false)
            .always_on_top(true)
            .build()
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}
