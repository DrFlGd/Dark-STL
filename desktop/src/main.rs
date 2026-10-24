// Dark STL as a desktop app: the page in src/darkstl/web in a window, with the Python
// engine behind it (`darkstl engine`, frozen into engine/ beside the app). The page's
// commands go to the engine over its stdin and stdout (src/darkstl/app/engine.py), so
// nothing listens on a port; meshes and pictures come through the engine:// protocol
// (http://engine.localhost/ on Windows). The window adds what needs it: the open dialogs
// and web links opened in the browser.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Manager, RunEvent, State};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;
use tokio::sync::oneshot;

enum Answer {
    Json(Value),
    Bytes(Vec<u8>, String),
    Error(u16, String),
}

const STOPPED: &str = "Dark STL's engine has stopped. Close Dark STL and start it again.";

/// The engine process and the requests waiting for its answers.
struct Engine {
    stdin: Mutex<Option<ChildStdin>>,
    child: Mutex<Option<Child>>,
    waiting: Mutex<HashMap<u64, oneshot::Sender<Answer>>>,
    next: AtomicU64,
    failed: Option<String>,
}

impl Engine {
    fn start(mut command: Command) -> Arc<Engine> {
        command.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(log_file());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW: no console beside the app
        }
        match command.spawn() {
            Ok(mut child) => {
                let stdin = child.stdin.take();
                let stdout = child.stdout.take().expect("piped stdout");
                let engine = Arc::new(Engine {
                    stdin: Mutex::new(stdin),
                    child: Mutex::new(Some(child)),
                    waiting: Mutex::new(HashMap::new()),
                    next: AtomicU64::new(1),
                    failed: None,
                });
                let reader = engine.clone();
                std::thread::spawn(move || reader.read_answers(stdout));
                engine
            }
            Err(e) => Arc::new(Engine {
                stdin: Mutex::new(None),
                child: Mutex::new(None),
                waiting: Mutex::new(HashMap::new()),
                next: AtomicU64::new(1),
                failed: Some(format!("Dark STL's engine didn't start ({:?}: {e}).", command.get_program())),
            }),
        }
    }

    /// One command; `bin` for the ones that answer with bytes.
    async fn call(&self, cmd: &str, args: Value, bin: bool) -> Answer {
        if let Some(e) = &self.failed {
            return Answer::Error(503, e.clone());
        }
        let id = self.next.fetch_add(1, Ordering::Relaxed);
        let (tx, rx) = oneshot::channel();
        self.waiting.lock().unwrap().insert(id, tx);
        let line = json!({ "id": id, "cmd": cmd, "args": args, "bin": bin }).to_string() + "\n";
        let sent = match self.stdin.lock().unwrap().as_mut() {
            Some(stdin) => stdin.write_all(line.as_bytes()).and_then(|_| stdin.flush()).is_ok(),
            None => false,
        };
        if !sent {
            self.waiting.lock().unwrap().remove(&id);
            return Answer::Error(503, STOPPED.into());
        }
        rx.await.unwrap_or_else(|_| Answer::Error(503, STOPPED.into()))
    }

    /// Hands each answer to the request waiting for it, until the engine's stdout ends.
    fn read_answers(&self, stdout: ChildStdout) {
        let mut out = BufReader::new(stdout);
        let mut line = String::new();
        loop {
            line.clear();
            if !matches!(out.read_line(&mut line), Ok(n) if n > 0) {
                break;
            }
            let Ok(head) = serde_json::from_str::<Value>(&line) else { continue };
            let answer = if let Some(size) = head.get("size").and_then(Value::as_u64) {
                let mut body = vec![0; size as usize];
                if out.read_exact(&mut body).is_err() {
                    break;
                }
                Answer::Bytes(body, head["type"].as_str().unwrap_or("application/octet-stream").into())
            } else if let Some(error) = head.get("error") {
                let status = head.get("status").and_then(Value::as_u64).unwrap_or(500) as u16;
                Answer::Error(status, error.as_str().unwrap_or("It didn't work.").into())
            } else {
                Answer::Json(head.get("result").cloned().unwrap_or(Value::Null))
            };
            let waiter = head["id"].as_u64().and_then(|id| self.waiting.lock().unwrap().remove(&id));
            if let Some(tx) = waiter {
                let _ = tx.send(answer);
            }
        }
        self.stdin.lock().unwrap().take();
        for (_, tx) in self.waiting.lock().unwrap().drain() {
            let _ = tx.send(Answer::Error(503, STOPPED.into()));
        }
    }

    fn stop(&self) {
        self.stdin.lock().unwrap().take(); // the engine ends when its input does
        if let Some(mut child) = self.child.lock().unwrap().take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// Where the engine's messages go: darkstl.log beside the engine's cache (app/config.py's cache_dir).
fn log_file() -> Stdio {
    let env = |k: &str| std::env::var_os(k).filter(|v| !v.is_empty()).map(PathBuf::from);
    let home = || env("USERPROFILE").or_else(|| env("HOME")).unwrap_or_default();
    let cache = if let Some(h) = env("DARKSTL_HOME") {
        h.join("cache")
    } else if cfg!(windows) {
        env("LOCALAPPDATA").unwrap_or_else(|| home().join("AppData").join("Local")).join("DarkSTL").join("cache")
    } else if cfg!(target_os = "macos") {
        home().join("Library").join("Caches").join("DarkSTL")
    } else {
        env("XDG_CACHE_HOME").unwrap_or_else(|| home().join(".cache")).join("darkstl")
    };
    let _ = std::fs::create_dir_all(&cache);
    File::create(cache.join("darkstl.log")).map(Stdio::from).unwrap_or_else(|_| Stdio::null())
}

/// `darkstl engine`: engine/darkstl(.exe) beside the app, or DARKSTL_ENGINE (an executable) when
/// set, for working on the app. Folders given to the app are added to its library; switches are
/// left out (whatever starts the app may add its own: WebDriver does, for the web view).
fn engine_command(app: &AppHandle) -> Command {
    let program = std::env::var_os("DARKSTL_ENGINE").map(PathBuf::from).unwrap_or_else(|| {
        let dir = app.path().resource_dir().unwrap_or_default().join("engine");
        dir.join(if cfg!(windows) { "darkstl.exe" } else { "darkstl" })
    });
    let mut command = Command::new(program);
    command.arg("engine").args(std::env::args_os().skip(1).filter(|a| !a.to_string_lossy().starts_with('-')));
    command
}

/// One of the engine's commands (src/darkstl/app/service.py), answered as JSON. The open dialogs
/// are the window's, so pick_folder and pick_file are answered here.
#[tauri::command]
async fn api(app: AppHandle, engine: State<'_, Arc<Engine>>, cmd: String, args: Value) -> Result<Value, String> {
    match cmd.as_str() {
        "pick_folder" => return Ok(json!({ "path": pick_folder(&app, &args) })),
        "pick_file" => return Ok(json!({ "path": pick_file(&app, &args) })),
        _ => {}
    }
    match engine.call(&cmd, args, false).await {
        Answer::Json(v) => Ok(v),
        Answer::Error(_, e) => Err(e),
        Answer::Bytes(..) => Err(format!("{cmd} answers with bytes: read it from engine://")),
    }
}

fn dialog_start(args: &Value) -> Option<PathBuf> {
    args["start"].as_str().filter(|s| !s.is_empty()).map(PathBuf::from).filter(|p| p.is_dir())
}

// (blocking dialogs are fine here: commands run off the main thread)
fn pick_folder(app: &AppHandle, args: &Value) -> Option<String> {
    let mut d = app.dialog().file().set_title(args["title"].as_str().unwrap_or("Choose a folder"));
    if let Some(start) = dialog_start(args) {
        d = d.set_directory(start);
    }
    d.blocking_pick_folder()?.into_path().ok().map(|p| p.display().to_string())
}

/// `patterns` as the engine gives them: "Name (*.a;*.b)", "All files (*.*)".
fn pick_file(app: &AppHandle, args: &Value) -> Option<String> {
    let mut d = app.dialog().file().set_title(args["title"].as_str().unwrap_or("Choose a file"));
    for pattern in args["patterns"].as_array().into_iter().flatten().filter_map(Value::as_str) {
        let Some((name, globs)) = pattern.split_once('(') else { continue };
        let exts: Vec<&str> = globs.trim_end_matches(')').split(';').filter_map(|g| g.trim().strip_prefix("*.")).collect();
        if !exts.is_empty() {
            d = d.add_filter(name.trim(), &exts);
        }
    }
    if let Some(start) = dialog_start(args) {
        d = d.set_directory(start);
    }
    d.blocking_pick_file()?.into_path().ok().map(|p| p.display().to_string())
}

/// engine://localhost/bin/<cmd>?<args>: a mesh or a picture, for fetch() and <img>.
fn engine_protocol(engine: Arc<Engine>, uri: String) -> impl std::future::Future<Output = tauri::http::Response<Vec<u8>>> {
    async move {
        let url = tauri::Url::parse(&uri).ok();
        let cmd = url.as_ref().and_then(|u| u.path().strip_prefix("/bin/").map(String::from)).unwrap_or_default();
        let args: Map<String, Value> =
            url.iter().flat_map(|u| u.query_pairs()).map(|(k, v)| (k.into_owned(), Value::String(v.into_owned()))).collect();
        let (status, ctype, body) = match engine.call(&cmd, Value::Object(args), true).await {
            Answer::Bytes(b, t) => (200, t, b),
            Answer::Error(s, e) => (s, "text/plain; charset=utf-8".into(), e.into_bytes()),
            Answer::Json(_) => (500, "text/plain".into(), b"Not bytes".to_vec()),
        };
        // a preview drawn already may be kept; an empty answer (not drawn yet) may not
        let cache = if cmd == "thumb" && status == 200 && !body.is_empty() { "private, max-age=3600" } else { "no-store" };
        tauri::http::Response::builder()
            .status(status)
            .header("Content-Type", ctype)
            .header("Cache-Control", cache)
            .header("Access-Control-Allow-Origin", "*")
            .body(body)
            .unwrap_or_else(|_| tauri::http::Response::new(Vec::new()))
    }
}

/// The app's own addresses: its page, the engine's pictures, the window's IPC.
fn in_app(url: &tauri::Url) -> bool {
    match url.scheme() {
        "tauri" | "engine" | "ipc" | "asset" | "about" | "blob" | "data" => true,
        "http" | "https" => url.host_str().is_some_and(|h| h == "localhost" || h.ends_with(".localhost")),
        _ => false,
    }
}

/// A web link the page tried to open: in the default browser, not in the app's window.
fn open_outside(app: &AppHandle, url: &tauri::Url) {
    if matches!(url.scheme(), "http" | "https" | "mailto") {
        let _ = app.opener().open_url(url.to_string(), None::<&str>);
    }
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .register_asynchronous_uri_scheme_protocol("engine", |ctx, request, responder| {
            let Some(engine) = ctx.app_handle().try_state::<Arc<Engine>>().map(|e| e.inner().clone()) else {
                return responder.respond(tauri::http::Response::builder().status(503).body(Vec::new()).unwrap());
            };
            let uri = request.uri().to_string();
            tauri::async_runtime::spawn(async move { responder.respond(engine_protocol(engine, uri).await) });
        })
        .setup(|app| {
            app.manage(Engine::start(engine_command(app.handle())));
            // the window (tauri.conf.json, "create": false), made here so that links leave it:
            // the web opens in the browser, a picture opened on its own gets a window of its own
            let cfg = app.config().app.windows.iter().find(|w| w.label == "main").cloned().ok_or("no main window")?;
            let (nav, popup) = (app.handle().clone(), app.handle().clone());
            tauri::WebviewWindowBuilder::from_config(app.handle(), &cfg)?
                .on_navigation(move |url| in_app(url) || {
                    open_outside(&nav, url);
                    false
                })
                .on_new_window(move |url, _features| {
                    if in_app(&url) {
                        return tauri::webview::NewWindowResponse::Allow;
                    }
                    open_outside(&popup, &url);
                    tauri::webview::NewWindowResponse::Deny
                })
                .build()?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![api])
        .build(tauri::generate_context!())
        .expect("error while starting Dark STL");
    app.run(|app, event| {
        if let (RunEvent::Exit, Some(engine)) = (event, app.try_state::<Arc<Engine>>()) {
            engine.stop();
        }
    });
}
