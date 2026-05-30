/// Tails launchd/stdout logs for Ollama, omnivoice, and comfyui.
/// Parses token-rate, synth RTF, and image-progress lines into LogMetrics
/// which the frontend polls via `get_log_metrics` command.
///
/// Log locations (Mac launchd stdout redirect):
///   Ollama    : ~/Library/Logs/ollama.log  (or journald on Linux)
///   omnivoice : ~/Library/Logs/omnivoice.log
///   comfyui   : ~/Library/Logs/comfyui.log
///
/// Patterns extracted:
///   Ollama    : "... X tokens/s ..."         → tokens_per_sec
///   omnivoice : "RTF: X.XX"                 → synth_rtf
///   comfyui   : "Progress: XX%"             → img_progress_pct

use std::collections::VecDeque;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};
use crate::models::LogMetric;

static METRICS: Mutex<VecDeque<LogMetric>> = Mutex::new(VecDeque::new());
const MAX_METRICS: usize = 120;

fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as u64
}

fn push(metric: LogMetric) {
    if let Ok(mut q) = METRICS.lock() {
        if q.len() >= MAX_METRICS {
            q.pop_front();
        }
        q.push_back(metric);
    }
}

fn log_path(name: &str) -> std::path::PathBuf {
    dirs_next::home_dir()
        .unwrap_or_default()
        .join("Library/Logs")
        .join(format!("{name}.log"))
}

/// Parse a single log line and push a metric if it matches.
pub fn parse_line(source: &str, line: &str) {
    match source {
        "ollama" => {
            // "llama_print_timings: eval time ... X tokens/s"
            // or simpler: "... 47.23 tokens/s ..."
            if let Some(pos) = line.find("tokens/s") {
                let before = &line[..pos];
                if let Some(num_str) = before.split_whitespace().last() {
                    if let Ok(v) = num_str.trim_matches(|c: char| !c.is_ascii_digit() && c != '.').parse::<f64>() {
                        push(LogMetric { source: "ollama".into(), metric: "tokens_per_sec".into(), value: v, ts: now_ms() });
                    }
                }
            }
        }
        "omnivoice" => {
            // "RTF: 0.62" or "real-time factor: 0.62"
            if let Some(pos) = line.to_lowercase().find("rtf:") {
                let after = &line[pos + 4..];
                if let Some(num_str) = after.split_whitespace().next() {
                    if let Ok(v) = num_str.trim().parse::<f64>() {
                        push(LogMetric { source: "omnivoice".into(), metric: "synth_rtf".into(), value: v, ts: now_ms() });
                    }
                }
            }
        }
        "comfyui" => {
            // "Progress: 45%" or "[45/100]"
            if let Some(pos) = line.find('[') {
                if let Some(end) = line[pos..].find(']') {
                    let inner = &line[pos+1..pos+end];
                    let parts: Vec<&str> = inner.split('/').collect();
                    if parts.len() == 2 {
                        if let (Ok(cur), Ok(tot)) = (parts[0].trim().parse::<f64>(), parts[1].trim().parse::<f64>()) {
                            if tot > 0.0 {
                                push(LogMetric { source: "comfyui".into(), metric: "img_progress_pct".into(), value: (cur / tot * 100.0).round(), ts: now_ms() });
                            }
                        }
                    }
                }
            }
        }
        _ => {}
    }
}

/// Drain all buffered metrics (called by `get_log_metrics` command).
pub fn drain_metrics() -> Vec<LogMetric> {
    METRICS.lock().map(|q| q.iter().cloned().collect()).unwrap_or_default()
}

/// Spawn background threads that tail each log file.
/// Called once from lib.rs on app startup.
pub fn start_watchers() {
    for source in &["ollama", "omnivoice", "comfyui"] {
        let source = source.to_string();
        let path = log_path(&source);
        std::thread::spawn(move || {
            tail_file(&source, &path);
        });
    }
}

fn tail_file(source: &str, path: &std::path::Path) {
    use std::io::{BufRead, Seek, SeekFrom};

    // Wait for the file to exist
    while !path.exists() {
        std::thread::sleep(std::time::Duration::from_secs(5));
    }

    let mut file = match std::fs::File::open(path) {
        Ok(f) => f,
        Err(_) => return,
    };
    // Seek to end so we only process new lines
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
