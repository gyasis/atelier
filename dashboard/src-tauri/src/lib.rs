mod models;
mod fetcher;
mod commands;
mod log_watcher;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // Start log-tail watchers before the window opens
    log_watcher::start_watchers();

    tauri::Builder::default()
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_pressure,
            commands::get_telemetry,
            commands::get_predictor_stats,
            commands::get_sidecars,
            commands::get_pressure_summary,
            commands::get_log_metrics,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
