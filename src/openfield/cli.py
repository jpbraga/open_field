"""openfield: run and manage the openField daemon.

  openfield serve [--port N] [--log-file PATH]   run the daemon in the foreground
  openfield check                                validate the config, show each service's availability
  openfield init [--port N] [--force]            create the user config from the bundled defaults
  openfield key [--new NAME [--role R]] [--revoke ID]   list, create or revoke access keys
  openfield skill [--url URL] [-o FILE]          the SKILL.md for consumer agents, from the live config
  openfield install-tools [--only NAME]          download Real-ESRGAN and RIFE for this platform
  openfield service install|uninstall|status     run at login (launchd / systemd --user / Task Scheduler)
"""
from __future__ import annotations

import argparse
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .config import APP_ROOT, Config, default_config_dir, expand

LABEL = "com.openfield.daemon"
TOOLS = {
    "realesrgan": ("v0.2.5.0 (20220424)", "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/"
                   "realesrgan-ncnn-vulkan-20220424-{os}.zip"),
    "rife": ("20221029 (rife-v4.6)", "https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/"
             "rife-ncnn-vulkan-20221029-{os}.zip"),
}


def user_config_dir() -> Path:
    if os.environ.get("OPENFIELD_CONFIG"):
        return expand(os.environ["OPENFIELD_CONFIG"])
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / "openfield"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "openfield"


# ---------------------------------------------------------------- init / check / key

def cmd_init(args: argparse.Namespace) -> None:
    dest = user_config_dir()
    if (dest / "daemon.yaml").exists() and not args.force:
        print(f"config already exists: {dest} (use --force to replace it with the defaults)")
    else:
        if dest.exists() and args.force:
            shutil.rmtree(dest)
        shutil.copytree(APP_ROOT / "config", dest)
        print(f"config created: {dest}")
    if args.port:
        daemon = dest / "daemon.yaml"
        lines = daemon.read_text().splitlines()
        daemon.write_text("\n".join(f"port: {args.port}" if ln.startswith("port:") else ln for ln in lines) + "\n")
        print(f"port set to {args.port}")


def cmd_check(args: argparse.Namespace) -> None:
    from .db import DB
    from .jobs import JobManager

    config = Config(args.config).load()
    manager = JobManager(config, DB(config.data_dir / "openfield.db"))
    print(f"config: {args.config}\nlisten: {config.daemon.host}:{config.daemon.port}  data: {config.data_dir}")
    for spec in config.services.values():
        ok, why = manager.availability(spec)
        print(f"  {'ok ' if ok else 'OFF'} {spec.id:28s} {spec.kind:11s} {spec.name}" + (f"  ({why})" if why else ""))
    for name, err in config.errors.items():
        print(f"  ERR {name}: {err}")
    sys.exit(1 if config.errors else 0)


def cmd_key(args: argparse.Namespace) -> None:
    import time

    from .auth import Auth, KeyStoreError
    from .db import DB

    config = Config(args.config).load()
    auth = Auth(config, DB(config.data_dir / "openfield.db"))
    try:
        if args.new:
            record, key = auth.create_key(args.new, args.role, "cli")
            print(f"new {record['role']} key {record['name']!r} ({record['id']}). Copy it now: only its hash is "
                  f"kept, so it is not shown again.\n{key}")
            return
        if args.revoke:
            item = auth.by_id(args.revoke) or next(
                (k for k in auth.keys().values() if k["source"] == "openfield" and k["name"] == args.revoke), None)
            if not item:
                sys.exit(f"no key {args.revoke!r} (see `openfield key`)")
            if item["source"] != "openfield":
                sys.exit("that key comes from oMLX: manage it in oMLX's settings")
            auth.delete_key(item["id"], "cli")
            print(f"revoked {item['name']!r} ({item['id']})")
            return
    except KeyStoreError as e:
        sys.exit(str(e))
    records = auth.records()
    if not records:
        record, key = auth.create_key("admin", "admin", "cli")
        print(f"no keys yet: created the admin key 'admin' ({record['id']}). Copy it now: it is not shown again."
              f"\n{key}")
        return
    fmt = "  {:10s} {:24s} {:6s} {:9s} {:8s} {:16s} {}"
    print(fmt.format("id", "name", "role", "source", "prefix", "last used", "state"))
    for r in records:
        used = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["last_used_at"])) if r.get("last_used_at") else "never"
        print(fmt.format(r["id"][:10], r["name"][:24], r["role"], r["source"], (r.get("prefix") or "-") + "…",
                         used, "disabled" if r.get("disabled") else "active"))
    print("Requests from this machine through localhost need no key. Manage keys in the web UI (Access tab) too.")


def cmd_skill(args: argparse.Namespace) -> None:
    from .agent_docs import render_skill
    from .db import DB
    from .jobs import JobManager
    from .media import gpu_name

    config = Config(args.config).load()
    if args.url:
        config.daemon.public_url = args.url
    manager = JobManager(config, DB(config.data_dir / "openfield.db"))
    text = render_skill(config, config.daemon.port, manager.availability, gpu_name())
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text)
        print(f"written: {args.output}")
    else:
        sys.stdout.write(text)


# ---------------------------------------------------------------- tools

def platform_tag() -> str:
    if sys.platform == "darwin":
        return "macos"
    if os.name == "nt":
        return "windows"
    return "ubuntu"


def cmd_install_tools(args: argparse.Namespace) -> None:
    tag = platform_tag()
    dest_root = APP_ROOT / "tools"
    dest_root.mkdir(exist_ok=True)
    for name, (version, url_tpl) in TOOLS.items():
        if args.only and name != args.only:
            continue
        url = url_tpl.format(os=tag)
        dest = dest_root / name
        print(f"{name}: downloading {url.rsplit('/', 1)[1]} ...", flush=True)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "tool.zip"
            urllib.request.urlretrieve(url, archive)
            out = Path(tmp) / "x"
            with zipfile.ZipFile(archive) as z:
                z.extractall(out)
            # RIFE's zip holds one top folder; Real-ESRGAN's files sit at the root.
            entries = [p for p in out.iterdir() if not p.name.startswith("__MACOSX")]
            root = entries[0] if len(entries) == 1 and entries[0].is_dir() else out
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(root), dest)
        if name == "rife":  # keep only the model openField uses (the zip ships ~400 MB of others)
            for model in dest.iterdir():
                if model.is_dir() and model.name.startswith("rife") and model.name != "rife-v4.6":
                    shutil.rmtree(model)
        for binary in dest.glob("*-ncnn-vulkan*"):
            if binary.is_file() and os.name != "nt":
                binary.chmod(0o755)
        if sys.platform == "darwin":
            subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(dest)], capture_output=True)
        (dest / "VERSION").write_text(version + "\n")
        print(f"{name}: installed in {dest}")
    if not shutil.which("ffmpeg"):
        print("note: ffmpeg is not on PATH; the video services need it "
              "(macOS: brew install ffmpeg; Linux: apt install ffmpeg; Windows: winget install Gyan.FFmpeg)")


# ---------------------------------------------------------------- service

def daemon_argv(config_dir: Path, log_file: Path) -> list[str]:
    python = Path(sys.executable)
    if os.name == "nt" and (python.parent / "pythonw.exe").exists():
        python = python.parent / "pythonw.exe"  # no console window
    return [str(python), "-m", "openfield", "--config", str(config_dir), "serve", "--log-file", str(log_file)]


def cmd_service(args: argparse.Namespace) -> None:
    config = Config(args.config).load()
    log_file = config.data_dir / "daemon.log"
    config.data_dir.mkdir(parents=True, exist_ok=True)
    argv = daemon_argv(args.config, log_file)
    system = platform.system()
    if system == "Darwin":
        plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
        domain = f"gui/{os.getuid()}"
        if args.action == "install":
            import plistlib

            plist.parent.mkdir(parents=True, exist_ok=True)
            plistlib.dump({"Label": LABEL, "ProgramArguments": argv, "WorkingDirectory": str(APP_ROOT),
                           "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                                    "PYTHONUNBUFFERED": "1"},
                           "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10, "ProcessType": "Interactive"},
                          plist.open("wb"))
            import time

            subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
            # bootout returns before the old job is gone; bootstrapping meanwhile fails with error 5 (EIO).
            for _ in range(40):
                if subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], capture_output=True).returncode:
                    break
                time.sleep(0.25)
            for attempt in range(5):
                r = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True, text=True)
                if r.returncode == 0:
                    break
                time.sleep(1 + attempt)
            else:
                sys.exit(f"launchctl bootstrap failed: {r.stderr.strip()}")
            print(f"launchd agent installed and started: {plist}")
        elif args.action == "uninstall":
            subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
            plist.unlink(missing_ok=True)
            print("launchd agent removed")
        else:
            r = subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], capture_output=True, text=True)
            print("running" if "state = running" in r.stdout else "not running")
    elif system == "Linux":
        unit = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user/openfield.service"
        if args.action == "install":
            unit.parent.mkdir(parents=True, exist_ok=True)
            quoted = " ".join(f'"{a}"' for a in argv)
            unit.write_text(f"[Unit]\nDescription=openField daemon\nAfter=network-online.target\n\n[Service]\n"
                            f"ExecStart={quoted}\nWorkingDirectory={APP_ROOT}\nRestart=always\nRestartSec=5\n"
                            f"Environment=PYTHONUNBUFFERED=1\n\n[Install]\nWantedBy=default.target\n")
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "enable", "--now", "openfield.service"], check=True)
            print(f"systemd user service installed and started: {unit}\n"
                  "to keep it running without a login session: loginctl enable-linger $USER")
        elif args.action == "uninstall":
            subprocess.run(["systemctl", "--user", "disable", "--now", "openfield.service"], capture_output=True)
            unit.unlink(missing_ok=True)
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            print("systemd user service removed")
        else:
            r = subprocess.run(["systemctl", "--user", "is-active", "openfield.service"], capture_output=True, text=True)
            print(r.stdout.strip() or "not installed")
    elif system == "Windows":
        def ps(script: str) -> subprocess.CompletedProcess:
            return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                  capture_output=True, text=True)

        def lit(s: str) -> str:
            return "'" + s.replace("'", "''") + "'"

        if args.action == "install":
            # Register-ScheduledTask has no length limit on the arguments (schtasks /TR stops at 261 characters);
            # no execution time limit (the default kills a task after 72 h) and a restart if it dies.
            script = (f"$a = New-ScheduledTaskAction -Execute {lit(argv[0])} -Argument {lit(subprocess.list2cmdline(argv[1:]))} "
                      f"-WorkingDirectory {lit(str(APP_ROOT))}; "
                      "$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; "
                      "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
                      "-ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1); "
                      "Register-ScheduledTask -TaskName 'openField' -Action $a -Trigger $t -Settings $s -Force | Out-Null; "
                      "Start-ScheduledTask -TaskName 'openField'")
            r = ps(script)
            if r.returncode:
                sys.exit(f"could not create the scheduled task: {r.stderr.strip()[-400:]}")
            print("scheduled task 'openField' installed (runs at logon) and started")
        elif args.action == "uninstall":
            ps("Stop-ScheduledTask -TaskName 'openField' -ErrorAction SilentlyContinue; "
               "Unregister-ScheduledTask -TaskName 'openField' -Confirm:$false -ErrorAction SilentlyContinue")
            print("scheduled task removed")
        else:
            r = ps("(Get-ScheduledTask -TaskName 'openField' -ErrorAction SilentlyContinue).State")
            print(r.stdout.strip() or "not installed")
    else:
        sys.exit(f"no service support for {system}; run `openfield serve` yourself")


# ---------------------------------------------------------------- serve

def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .api import create_app

    if args.log_file:
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                            filename=args.log_file, force=True)
    daemon = Config(args.config).load().daemon
    port = args.port or daemon.port
    uvicorn.run(create_app(args.config, port), host=args.host or daemon.host, port=port, log_level="info",
                proxy_headers=False, log_config=None if args.log_file else uvicorn.config.LOGGING_CONFIG)


def main() -> None:
    parser = argparse.ArgumentParser(prog="openfield", description="openField daemon")
    parser.add_argument("--config", type=Path, default=None, help="config folder (daemon.yaml, services/)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the daemon")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--log-file")
    sub.add_parser("check", help="validate the config and show each service's availability")
    init = sub.add_parser("init", help="create the user config from the bundled defaults")
    init.add_argument("--port", type=int)
    init.add_argument("--force", action="store_true")
    key = sub.add_parser("key", help="list, create or revoke access keys for other machines")
    key.add_argument("--new", metavar="NAME", help="create a key (shown once)")
    key.add_argument("--role", choices=["agent", "admin"], default="agent",
                     help="agent: API, MCP and proxies; admin: also keys and settings (default agent)")
    key.add_argument("--revoke", metavar="ID_OR_NAME")
    skill = sub.add_parser("skill", help="print the SKILL.md for consumer agents")
    skill.add_argument("--url", help="base URL agents use (default: public_url of daemon.yaml)")
    skill.add_argument("-o", "--output", help="write to this file instead of stdout")
    tools = sub.add_parser("install-tools", help="download Real-ESRGAN and RIFE for this platform")
    tools.add_argument("--only", choices=sorted(TOOLS))
    service = sub.add_parser("service", help="run openField at login")
    service.add_argument("action", choices=["install", "uninstall", "status"])
    args = parser.parse_args()
    args.config = (args.config or default_config_dir()).expanduser()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    {"serve": cmd_serve, "check": cmd_check, "init": cmd_init, "key": cmd_key, "skill": cmd_skill, "install-tools": cmd_install_tools,
     "service": cmd_service}[args.cmd](args)


if __name__ == "__main__":
    main()
