//! Diagnostic for #1220's current topology: Job cancellation versus an intentional BREAK0.
//! Every owned root uses LocalProcessRunner; the two plain children are ordinary tool leaves.
use std::collections::BTreeMap;
use std::path::Path;
use std::process::{Child, Stdio};

use serde::{Deserialize, Serialize};
use tokio::sync::watch;
use tokio::time::{Duration, timeout};
use windows_sys::Win32::Foundation::STATUS_CONTROL_C_EXIT;
use windows_sys::Win32::System::Console::{
    CTRL_BREAK_EVENT, GenerateConsoleCtrlEvent, GetConsoleCP, GetConsoleProcessList,
};
use windows_sys::Win32::System::JobObjects::JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

use crate::execution::WorkspaceAccessMode;
use crate::execution::driver::{DriverCancellation, WorkspaceCapability};
use crate::execution::process::{
    LocalProcessRunner, ProcessSession, ProcessSessionCommand, ProcessSessionOutput,
};
use crate::execution::process::platform::ProcessCleanupEvidence;

use super::super::{guard_controller_console, spawn_controller};
use super::{console_window_visible, fixture_command, join_job, wait_for};

const MODE: &str = "ZEROSHOT_CONSOLE_PROBE_MODE";
const DIRECTORY: &str = "ZEROSHOT_CONSOLE_PROBE_DIRECTORY";
const TEST: &str = concat!(
    "execution::platform::windows::controller::tests::",
    "console_probe_tests::owned_job_and_break0_diagnostic"
);
const LIMIT: Duration = Duration::from_secs(10);

#[derive(Deserialize, Serialize)]
struct ConsoleFacts {
    pid: u32,
    members: Vec<u32>,
    visible: bool,
}

#[derive(Deserialize, Serialize)]
struct LeafExits {
    sender: u32,
    control: u32,
}

#[derive(Deserialize, Serialize)]
struct Evidence {
    operation: String,
    controller: u32,
    controller_survived: bool,
    a: u32,
    b: u32,
    sender: u32,
    control: u32,
    console_members: Vec<u32>,
    a_job_cancelled: bool,
    b_survived: bool,
    b_exit: Option<u32>,
    leaf_exits: Option<LeafExits>,
}

#[tokio::test]
async fn owned_job_and_break0_diagnostic() {
    if let Ok(mode) = std::env::var(MODE) {
        let directory = std::path::PathBuf::from(std::env::var_os(DIRECTORY).unwrap());
        match mode.as_str() {
            "controller" => controller_fixture(&directory).await,
            "a" => a_fixture(&directory),
            "sender" => sender_fixture(&directory),
            "b" | "control" => idle_fixture(&directory, &mode),
            _ => panic!("unknown console probe mode"),
        }
        return;
    }
    let scratch = tempfile::tempdir().unwrap();
    let directory = scratch.path().join("private");
    crate::execution::platform::create_private_directory(&directory).unwrap();
    let mut command = fixture_command(TEST, MODE, "controller");
    command.env(DIRECTORY, &directory);
    let mut controller = spawn_controller(&mut command).unwrap();
    let exited = wait_for(Duration::from_secs(60), || {
        controller.try_wait().unwrap().is_some()
    })
    .await;
    controller.kill().await.unwrap();
    assert!(
        exited && directory.join("evidence.json").is_file(),
        "console probe failed: {}",
        std::fs::read_to_string(directory.join("failure.txt"))
            .unwrap_or_else(|_| "controller did not finish".into())
    );
    let evidence: Vec<Evidence> = read_json(&directory.join("evidence.json"));
    assert_eq!(evidence.len(), 2);
    for record in evidence {
        let line = format!(
            "console-probe {}\n",
            serde_json::to_string(&record).unwrap()
        );
        std::io::Write::write_all(&mut std::io::stdout().lock(), line.as_bytes()).unwrap();
    }
}

async fn controller_fixture(directory: &Path) {
    guard_controller_console().unwrap();
    // Closing the last handle on process exit collects every fixture, including panic/timeout paths.
    std::mem::forget(join_job(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE));
    let failure = directory.join("failure.txt");
    std::panic::set_hook(Box::new(move |info| {
        let detail: String = info.to_string().chars().take(800).collect();
        let _ = std::fs::write(&failure, detail);
    }));
    assert_ne!(unsafe { GetConsoleCP() }, 0);
    assert!(!console_window_visible());
    let mut evidence = Vec::new();
    for broadcast in [false, true] {
        let case = directory.join(if broadcast { "break0" } else { "job" });
        crate::execution::platform::private_directory(&case).unwrap();
        evidence.push(run_case(&case, broadcast).await);
    }
    write_json(&directory.join("evidence.json"), &evidence);
}

async fn run_case(directory: &Path, broadcast: bool) -> Evidence {
    let mut a = Owned::open(directory, "a").await;
    let mut b = Owned::open(directory, "b").await;
    let a_facts: ConsoleFacts = read_json(&directory.join("a.json"));
    let b_facts: ConsoleFacts = read_json(&directory.join("b.json"));
    let sender: ConsoleFacts = read_json(&directory.join("sender.json"));
    let control: ConsoleFacts = read_json(&directory.join("control.json"));
    let controller = std::process::id();
    for pid in [
        controller,
        a_facts.pid,
        b_facts.pid,
        sender.pid,
        control.pid,
    ] {
        assert!(
            b_facts.members.contains(&pid),
            "missing console member {pid}"
        );
    }
    assert!(!a_facts.visible && !b_facts.visible);
    let (b_exit, leaf_exits) = if broadcast {
        std::fs::write(directory.join("broadcast"), b"go").unwrap();
        await_file(&directory.join("leaf-exits.json")).await;
        let leaves: LeafExits = read_json(&directory.join("leaf-exits.json"));
        assert_eq!(leaves.sender, STATUS_CONTROL_C_EXIT as u32);
        assert_eq!(leaves.control, STATUS_CONTROL_C_EXIT as u32);
        let outcome = b.wait().await;
        assert!(!outcome.cancelled && !outcome.timed_out);
        assert_eq!(
            outcome.exit_code.map(|code| code as u32),
            Some(STATUS_CONTROL_C_EXIT as u32)
        );
        (outcome.exit_code.map(|code| code as u32), Some(leaves))
    } else {
        let outcome = a.cancel().await;
        assert!(outcome.cancelled && !outcome.timed_out);
        assert_eq!(outcome.cleanup, ProcessCleanupEvidence::Reaped);
        std::fs::write(directory.join("ping"), b"ping").unwrap();
        await_file(&directory.join("pong")).await;
        assert!(
            timeout(Duration::from_millis(50), b.process.wait())
                .await
                .is_err()
        );
        (None, None)
    };
    // The controller reaches this after either operation. No model process or token is involved.
    let record = Evidence {
        operation: if broadcast {
            "intentional_BREAK0"
        } else {
            "owned_Job_cancellation"
        }
        .into(),
        controller,
        controller_survived: true,
        a: a_facts.pid,
        b: b_facts.pid,
        sender: sender.pid,
        control: control.pid,
        console_members: b_facts.members,
        a_job_cancelled: !broadcast,
        b_survived: !broadcast,
        b_exit,
        leaf_exits,
    };
    a.cancel().await;
    b.cancel().await;
    record
}

struct Owned {
    cancellation: watch::Sender<bool>,
    process: ProcessSession,
}

impl Owned {
    async fn open(directory: &Path, mode: &str) -> Self {
        let (cancellation, receiver) = watch::channel(false);
        let mut environment = BTreeMap::from([
            (MODE.to_owned(), mode.to_owned()),
            (DIRECTORY.to_owned(), directory.to_str().unwrap().to_owned()),
        ]);
        if let Ok(system_root) = std::env::var("SystemRoot") {
            environment.insert("SystemRoot".into(), system_root);
        }
        let command = ProcessSessionCommand {
            program: std::env::current_exe()
                .unwrap()
                .to_str()
                .unwrap()
                .to_owned(),
            argv: vec!["--exact".into(), TEST.into(), "--nocapture".into()],
            environment,
            workspace: WorkspaceCapability {
                current_dir: directory.to_owned(),
                mode: WorkspaceAccessMode::ReadWrite,
            },
            deadline: Some(tokio::time::Instant::now() + Duration::from_secs(35)),
        };
        let mut process = LocalProcessRunner::new()
            .open(command, DriverCancellation::new(receiver))
            .await
            .unwrap();
        let mut stdout = process.detach_stdout();
        tokio::spawn(async move { while stdout.recv().await.is_some() {} });
        await_file(&directory.join(format!("{mode}.json"))).await;
        Self {
            cancellation,
            process,
        }
    }

    async fn wait(&mut self) -> ProcessSessionOutput {
        let output = timeout(LIMIT, self.process.wait()).await.unwrap().unwrap();
        assert!(output.cleanup.proves_tree_empty());
        output
    }

    async fn cancel(&mut self) -> ProcessSessionOutput {
        self.cancellation.send_replace(true);
        self.wait().await
    }
}

fn a_fixture(directory: &Path) {
    // Only A's root catches BREAK0; tool leaves remain unguarded. This avoids Job cleanup racing
    // the positive control and also models a harness that catches a signal its tools do not.
    guard_controller_console().unwrap();
    let mut control = plain_leaf(directory, "control");
    let mut sender = plain_leaf(directory, "sender");
    wait_file_blocking(&directory.join("control.json"));
    wait_file_blocking(&directory.join("sender.json"));
    write_facts(directory, "a");
    let deadline = std::time::Instant::now() + Duration::from_secs(35);
    loop {
        if directory.join("broadcast").exists() && !directory.join("leaf-exits.json").exists() {
            if let Some(exits) = leaf_exits(&mut sender, &mut control) {
                write_json(&directory.join("leaf-exits.json"), &exits);
            }
        }
        assert!(
            std::time::Instant::now() < deadline,
            "A fixture exceeded its limit"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}

struct ToolLeaf(Child);

impl Drop for ToolLeaf {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn plain_leaf(directory: &Path, mode: &str) -> ToolLeaf {
    let mut command = fixture_command(TEST, MODE, mode).into_std();
    command
        .env(DIRECTORY, directory)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    ToolLeaf(command.spawn().unwrap())
}

fn leaf_exits(sender: &mut ToolLeaf, control: &mut ToolLeaf) -> Option<LeafExits> {
    let sender = sender.0.try_wait().unwrap()?.code().unwrap() as u32;
    let control = control.0.try_wait().unwrap()?.code().unwrap() as u32;
    Some(LeafExits { sender, control })
}

fn sender_fixture(directory: &Path) {
    write_facts(directory, "sender");
    wait_file_blocking(&directory.join("broadcast"));
    assert_ne!(unsafe { GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, 0) }, 0);
    // A delivered event terminates this unguarded ordinary tool leaf.
    std::thread::sleep(LIMIT);
    panic!("unguarded broadcaster survived BREAK0");
}

fn idle_fixture(directory: &Path, mode: &str) {
    write_facts(directory, mode);
    let deadline = std::time::Instant::now() + Duration::from_secs(35);
    loop {
        if mode == "b" && directory.join("ping").exists() {
            std::fs::write(directory.join("pong"), b"alive").unwrap();
        }
        assert!(
            std::time::Instant::now() < deadline,
            "idle fixture exceeded its limit"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}

fn write_facts(directory: &Path, mode: &str) {
    assert_ne!(unsafe { GetConsoleCP() }, 0);
    let mut members = [0_u32; 128];
    let count = unsafe { GetConsoleProcessList(members.as_mut_ptr(), members.len() as u32) };
    assert!(count > 0 && count as usize <= members.len());
    write_json(
        &directory.join(format!("{mode}.json")),
        &ConsoleFacts {
            pid: std::process::id(),
            members: members[..count as usize].to_vec(),
            visible: console_window_visible(),
        },
    );
}

async fn await_file(path: &Path) {
    assert!(
        wait_for(LIMIT, || path.is_file()).await,
        "missing fixture file {path:?}"
    );
}

fn wait_file_blocking(path: &Path) {
    let deadline = std::time::Instant::now() + LIMIT;
    while !path.is_file() {
        assert!(
            std::time::Instant::now() < deadline,
            "missing fixture file {path:?}"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}

fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> T {
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

fn write_json(path: &Path, value: &impl Serialize) {
    let temporary = path.with_extension("tmp");
    std::fs::write(&temporary, serde_json::to_vec(value).unwrap()).unwrap();
    std::fs::rename(temporary, path).unwrap();
}
