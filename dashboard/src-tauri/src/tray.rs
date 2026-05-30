use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, Runtime,
};

pub fn setup_tray<R: Runtime>(app: &tauri::App<R>) -> tauri::Result<()> {
    let open_dash  = MenuItem::with_id(app, "open_dash",  "Open Dashboard", true, None::<&str>)?;
    let open_hud   = MenuItem::with_id(app, "open_hud",   "Open Mini HUD",  true, None::<&str>)?;
    let sep        = PredefinedMenuItem::separator(app)?;
    let quit       = MenuItem::with_id(app, "quit",       "Quit Atelier",   true, None::<&str>)?;

    let menu = Menu::with_items(app, &[&open_dash, &open_hud, &sep, &quit])?;

    let _tray = TrayIconBuilder::with_id("atelier-tray")
        .icon(app.default_window_icon().cloned().unwrap())
        .menu(&menu)
        .menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open_dash" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
            "open_hud" => {
                if app.get_webview_window("hud").is_none() {
                    let _ = tauri::WebviewWindowBuilder::new(app, "hud", tauri::WebviewUrl::App("hud.html".into()))
                        .title("Atelier Mini HUD")
                        .inner_size(420.0, 360.0)
                        .resizable(false)
                        .build();
                } else if let Some(w) = app.get_webview_window("hud") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                let app = tray.app_handle();
                if let Some(w) = app.get_webview_window("main") {
                    if w.is_visible().unwrap_or(false) {
                        let _ = w.hide();
                    } else {
                        let _ = w.show();
                        let _ = w.set_focus();
                    }
                }
            }
        })
        .build(app)?;

    Ok(())
}
