#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

def get_git_commit() -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return "unknown"

def check_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def main():
    head_commit = get_git_commit()
    
    runtime_dir = PROJECT_ROOT / "logs" / "runtime"
    listener_path = runtime_dir / "telegram_listener_identity.json"
    scanner_path = runtime_dir / "scanner_identity.json"
    
    listener_info = None
    if listener_path.exists():
        try:
            with listener_path.open("r", encoding="utf-8") as handle:
                listener_info = json.load(handle)
        except Exception:
            pass
            
    scanner_info = None
    if scanner_path.exists():
        try:
            with scanner_path.open("r", encoding="utf-8") as handle:
                scanner_info = json.load(handle)
        except Exception:
            pass

    print("=== Runtime Freshness Report ===")
    print(f"Current Repository HEAD: {head_commit}")
    
    if scanner_info:
        scan_commit = scanner_info.get("commit", "unknown")
        scan_time = scanner_info.get("start_time", "unknown")
        scan_root = scanner_info.get("project_root", "unknown")
        print(f"Scanner Latest Run Commit: {scan_commit}")
        print(f"Scanner Latest Run Time: {scan_time}")
        print(f"Scanner Project Root: {scan_root}")
        if scan_root != str(PROJECT_ROOT):
            print(f"⚠️ Scanner Project Root Mismatch: {scan_root} vs expected {PROJECT_ROOT}")
    else:
        print("Scanner status: No run recorded yet.")
        
    listener_stale = False
    if listener_info:
        list_pid = listener_info.get("pid")
        list_commit = listener_info.get("commit", "unknown")
        list_time = listener_info.get("start_time", "unknown")
        list_root = listener_info.get("project_root", "unknown")
        
        running = check_pid_running(list_pid) if list_pid else False
        
        print(f"Listener PID: {list_pid} ({'RUNNING' if running else 'NOT RUNNING'})")
        print(f"Listener Startup Commit: {list_commit}")
        print(f"Listener Start Time: {list_time}")
        print(f"Listener Project Root: {list_root}")
        
        if running:
            if list_root != str(PROJECT_ROOT):
                print(f"⚠️ Listener Project Root Mismatch: {list_root} vs expected {PROJECT_ROOT}")
            if head_commit != "unknown" and list_commit != "unknown" and list_commit != head_commit:
                listener_stale = True
                print("❌ Listener status: STALE (Running code differs from current HEAD)")
            else:
                print("✅ Listener status: FRESH (Matches repository HEAD)")
        else:
            print("❌ Listener status: NOT RUNNING")
    else:
        print("Listener status: No startup identity recorded.")
        listener_stale = True
        
    print("--------------------------------")
    if listener_stale:
        print("Telegram listener is stale and must be restarted")
        print("To restart safely, run:")
        print("  ./scripts/restart_telegram_listener.sh")
        sys.exit(1)
    else:
        sys.exit(0)

if __name__ == "__main__":
    main()
