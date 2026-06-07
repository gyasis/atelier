/// Tails Ollama server log + sidecar logs for live metrics.
///
/// Ollama log: ~/.ollama/logs/server.log  (GIN HTTP log + sched/server events)
///   - Tracks active model from: msg="loading model" (sched.go lines)
///   - Parses GIN lines: [GIN] ... | 200 | 1.2s | 127.0.0.1 | POST "/api/chat"
///     → emits LogMetric { source:"ollama", metric:"model_call",
///                         label:"<model>", path:"/api/chat", latency_ms }
///   - Also parses tokens/s from eval timing lines
/// omnivoice: ~/Library/Logs/omnivoice-sidecar.out.log
///   - "[tts] chars=N ... rtf=X.XXx"  → synth_rtf
/// comfyui: ~/Library/Logs/comfyui.out.log
///   - "[N/M]"  → img_progress_pct

use std::collections::VecDeque;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};
use crate::models::LogMetric;

// Tracks the currently active Ollama model (updated by "loading model" log lines)
static CURRENT_OLLAMA_MODEL: Mutex<String> = Mutex::new(String::new());

static METRICS: Mutex<VecDeque<LogMetric>> = Mutex::new(VecDeque::new());
const MAX_METRICS: usize = 120;

fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64
}

fn push(metric: LogMetric) {
    if let Ok(mut q) = METRICS.lock() {
        if q.len() >= MAX_METRICS { q.pop_front(); }
        q.push_back(metric);
    }
}

fn simple_metric(source: &str, metric: &str, value: f64) -> LogMetric {
    LogMetric { source: source.into(), metric: metric.into(), value, ts: now_ms(), label: None, path: None, status: None }
}

fn log_path(name: &str) -> std::path::PathBuf {
    let home = dirs_next::home_dir().unwrap_or_default();
    match name {
        "omnivoice" => home.join("Library/Logs/omnivoice-sidecar.out.log"),
        "whisper"   => home.join("Library/Logs/whisper-sidecar.out.log"),
        "comfyui"   => home.join("Library/Logs/comfyui.out.log"),
        "ollama"    => home.join(".ollama/logs/server.log"),
        _           => home.join(format!("Library/Logs/{name}.log")),
    }
}

/// Parse a single log line and push a metric if it matches.
pub fn parse_line(source: &str, line: &str) {
    match source {
        "ollama" => {
            // Track active model from scheduler lines:
            // time=... source=sched.go msg="loaded runners" model=gemma4:latest
            // time=... source=server.go msg="starting runner" cmd="... --model /path/sha256-..."
            // simpler: look for model name in "msg=\"new model\"" or after "model=" in sched lines
            if line.contains("msg=\"new model\"") || line.contains("msg=\"loaded runners\"") {
                if let Some(pos) = line.find("model=") {
                    let after = &line[pos+6..];
                    let name = after.split_whitespace().next().unwrap_or("").trim_matches('"');
                    if !name.is_empty() {
                        if let Ok(mut m) = CURRENT_OLLAMA_MODEL.lock() { *m = name.to_string(); }
                    }
                }
            }
            // Track model from /api/ps style loading:
            // time=... source=sched.go:627 msg="updated VRAM ..." (don't extract from here)
            // "[GIN] 2026/05/31 - 00:30:15 | 200 | 7.074860167s | 127.0.0.1 | POST     \"/api/generate\""
            if line.contains("[GIN]") && (line.contains("/api/generate") || line.contains("/api/chat") || line.contains("/api/embed")) {
                let model = CURRENT_OLLAMA_MODEL.lock().ok()
                    .map(|m| if m.is_empty() { "ollama".to_string() } else { m.clone() })
                    .unwrap_or_else(|| "ollama".to_string());

                // Parse status code (field after second |)
                let parts: Vec<&str> = line.split('|').collect();
                let status = parts.get(1).and_then(|s| s.trim().parse::<u16>().ok()).unwrap_or(200);

                // Parse latency (field after third |) — "7.074860167s" or "1m39s"
                let latency_str = parts.get(2).map(|s| s.trim()).unwrap_or("");
                let latency_ms = parse_duration_ms(latency_str);

                // Parse path (last field — strip quotes)
                let path = parts.last()
                    .map(|s| s.trim().trim_matches('"').to_string())
                    .unwrap_or_else(|| "/api/generate".to_string());

                push(LogMetric {
                    source: "ollama".into(), metric: "model_call".into(),
                    value: latency_ms, ts: now_ms(),
                    label: Some(model), path: Some(path), status: Some(status),
                });
            }
            // tokens/s from eval timing
            if let Some(pos) = line.find("tokens/s") {
                let before = &line[..pos];
                if let Some(num_str) = before.split_whitespace().last() {
                    let clean: String = num_str.chars().filter(|c| c.is_ascii_digit() || *c == '.').collect();
                    if let Ok(v) = clean.parse::<f64>() {
                        if v > 0.0 { push(simple_metric("ollama", "tokens_per_sec", v)); }
                    }
                }
            }
        }
        "omnivoice" => {
            // "[tts] chars=487 num_step=48 64.94s rtf=1.89x"
            if let Some(pos) = line.find("rtf=") {
                let after = &line[pos + 4..];
                let num_str = after.trim_end_matches('x').split_whitespace().next().unwrap_or("");
                if let Ok(v) = num_str.parse::<f64>() {
                    push(simple_metric("omnivoice", "synth_rtf", v));
                }
            }
        }
        "whisper" => {
            // "[asr] model=whisper-large-v3-turbo audio_s=14.0 ... 1.60s rtf=8.7x lang=en"
            if let Some(pos) = line.find("rtf=") {
                let after = &line[pos + 4..];
                let num_str = after.trim_end_matches('x').split_whitespace().next().unwrap_or("");
                if let Ok(v) = num_str.parse::<f64>() {
                    push(simple_metric("whisper", "asr_rtf", v));
                }
            }
        }
        "comfyui" => {
            // "[45/100]"
            if let Some(pos) = line.find('[') {
                if let Some(end) = line[pos..].find(']') {
                    let inner = &line[pos+1..pos+end];
                    let parts: Vec<&str> = inner.split('/').collect();
                    if parts.len() == 2 {
                        if let (Ok(cur), Ok(tot)) = (parts[0].trim().parse::<f64>(), parts[1].trim().parse::<f64>()) {
                            if tot > 0.0 {
                                push(simple_metric("comfyui", "img_progress_pct", (cur/tot*100.0).round()));
                            }
                        }
                    }
                }
            }
        }
        _ => {}
    }
}

fn parse_duration_ms(s: &str) -> f64 {
    // "7.074860167s" → 7074.86  |  "1m39s" → 99000  |  "200ms" → 200
    if s.contains('m') {
        let parts: Vec<&str> = s.splitn(2, 'm').collect();
        let mins: f64 = parts[0].parse().unwrap_or(0.0);
        let secs: f64 = parts.get(1).map(|s| s.trim_end_matches('s').parse().unwrap_or(0.0)).unwrap_or(0.0);
        (mins * 60.0 + secs) * 1000.0
    } else if s.ends_with("ms") {
        s.trim_end_matches("ms").parse().unwrap_or(0.0)
    } else if s.ends_with('s') {
        s.trim_end_matches('s').parse::<f64>().unwrap_or(0.0) * 1000.0
    } else {
        0.0
    }
}

/// Drain all buffered metrics (called by `get_log_metrics` command).
pub fn drain_metrics() -> Vec<LogMetric> {
    METRICS.lock().map(|q| q.iter().cloned().collect()).unwrap_or_default()
}

/// Spawn background threads that tail each log file.
/// Called once from lib.rs on app startup.
pub fn start_watchers() {
    for source in &["ollama", "omnivoice", "whisper", "comfyui"] {
        let source = source.to_string();
        let path = log_path(&source);
        std::thread::spawn(move || {
            tail_file(&source, &path);
        });
    }
}

/// Read the last `max_bytes` of the file, split into lines, return them.
fn read_tail_bytes(path: &std::path::Path, max_bytes: u64) -> Vec<String> {
    use std::io::{Read, Seek, SeekFrom};
    let mut file = match std::fs::File::open(path) { Ok(f) => f, Err(_) => return vec![] };
    let size = file.seek(SeekFrom::End(0)).unwrap_or(0);
    let start = size.saturating_sub(max_bytes);
    let _ = file.seek(SeekFrom::Start(start));
    let mut buf = String::new();
    let _ = file.read_to_string(&mut buf);
    buf.lines().map(|l| l.to_string()).collect()
}

fn tail_file(source: &str, path: &std::path::Path) {
    use std::io::{BufRead, Seek, SeekFrom};

    // Wait for the file to exist
    while !path.exists() {
        std::thread::sleep(std::time::Duration::from_secs(5));
    }

    // --- Backfill: parse last ~512KB of log (typically covers 60+ min) ---
    let historical = read_tail_bytes(path, 4 * 1024 * 1024);   // 4MB — survive the /api/ps flood
    let backfill_count = historical.len();
    for line in &historical {
        parse_line(source, line);
    }
    if backfill_count > 0 {
        log::info!("[log_watcher] {source}: backfilled {backfill_count} lines from {path:?}");
    }

    // --- Now tail from current end ---
    let mut file = match std::fs::File::open(path) {
        Ok(f) => f,
        Err(_) => return,
    };
    let _ = file.seek(SeekFrom::End(0));

    let mut reader = std::io::BufReader::new(file);
    let mut line = String::new();
    loop {
        line.clear();
        match reader.read_line(&mut line) {
            Ok(0) => std::thread::sleep(std::time::Duration::from_millis(200)),
            Ok(_) => parse_line(source, &line),
            Err(_) => std::thread::sleep(std::time::Duration::from_secs(1)),
        }
    }
}
