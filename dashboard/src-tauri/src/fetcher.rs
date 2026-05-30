use reqwest::Client;
use crate::models::*;

const GOVERNOR: &str = "http://127.0.0.1:8799";
const OLLAMA:   &str = "http://127.0.0.1:11434";
const TIMEOUT:   u64 = 3; // seconds

fn client() -> Client {
    Client::builder()
        .timeout(std::time::Duration::from_secs(TIMEOUT))
        .build()
        .unwrap_or_default()
}

pub async fn fetch_pressure() -> PressureResponse {
    let c = client();
    match c.get(format!("{GOVERNOR}/pressure")).send().await {
        Ok(r) => r.json::<PressureResponse>().await.unwrap_or_default(),
        Err(_) => PressureResponse::default(),
    }
}

pub async fn fetch_telemetry() -> TelemetryResponse {
    let c = client();
    match c.get(format!("{GOVERNOR}/telemetry")).send().await {
        Ok(r) => r.json::<TelemetryResponse>().await.unwrap_or_default(),
        Err(_) => TelemetryResponse::default(),
    }
}

pub async fn fetch_predictor_stats() -> PredictorStatsResponse {
    let c = client();
    match c.get(format!("{GOVERNOR}/predictor/stats")).send().await {
        Ok(r) => r.json::<PredictorStatsResponse>().await.unwrap_or_default(),
        Err(_) => PredictorStatsResponse::default(),
    }
}

pub async fn fetch_sidecar(port: u16) -> SidecarReadyz {
    let c = client();
    match c.get(format!("http://127.0.0.1:{port}/readyz")).send().await {
        Ok(r) => r.json::<SidecarReadyz>().await.unwrap_or_default(),
        Err(_) => SidecarReadyz::default(),
    }
}

pub async fn fetch_ollama_ps() -> serde_json::Value {
    let c = client();
    match c.get(format!("{OLLAMA}/api/ps")).send().await {
        Ok(r) => r.json::<serde_json::Value>().await.unwrap_or(serde_json::json!({"models":[]})),
        Err(_) => serde_json::json!({"models":[]}),
    }
}
