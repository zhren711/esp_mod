from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def map_state(event: dict) -> str | None:
    name = event.get("hook_event_name", "")
    tool = event.get("tool_name", "")
    if name == "SessionStart":
        return "IDLE"
    if name == "UserPromptSubmit":
        return "THINKING"
    if name == "PreToolUse":
        return "WRITING" if tool == "apply_patch" else "RUNNING"
    if name == "PostToolUse":
        response = json.dumps(event.get("tool_response"), ensure_ascii=False).lower()
        return "ERROR" if any(x in response for x in ("error", "failed", "exit code: 1", "exit status 1")) else "THINKING"
    if name == "PermissionRequest":
        return "NEED_CONFIRM"
    if name == "Stop":
        return "DONE"
    if name in ("PreCompact", "PostCompact"):
        return "THINKING"
    return None


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    state = map_state(event)
    if state:
        target = codex_home() / "lcd_hook_state.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"state": state, "updated_at": time.time(), "event": event.get("hook_event_name", "")}),
            encoding="utf-8",
        )
        os.replace(temporary, target)
    if event.get("hook_event_name") == "Stop":
        print(json.dumps({"continue": True}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(0)

