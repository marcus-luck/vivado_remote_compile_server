"""Claude Code hooks that keep the ClickUp progress tasks in view.

The hooks never talk to ClickUp themselves; they tell Claude to do it through
the clickup-tasks skill and the ClickUp connector.

Usage, from .claude/settings.json:

    clickup_tasks.py session-start  SessionStart: ask Claude to list the tasks.
    clickup_tasks.py mark-edit      PostToolUse on file edits: note that this
                                    reply changed files.
    clickup_tasks.py stop           Stop: if the reply changed files, ask
                                    Claude to update the matching tasks.

Each mode reads the hook's JSON input from stdin.
"""

import json
import pathlib
import sys
import tempfile

_SESSION_START_CONTEXT = (
    "Progress on this project is tracked in ClickUp (list FW_build_server). "
    "Use the clickup-tasks skill to list the open tasks once at the start of "
    "this session, and keep them in mind while working."
)
_STOP_REASON = (
    "This reply changed files. Using the clickup-tasks skill, set any open "
    "ClickUp task this work clearly started to 'in progress' and any it fully "
    "finished to 'complete'. Do not create tasks. If no task matches, finish "
    "without mentioning ClickUp."
)


def _marker_path(session_id: str) -> pathlib.Path:
    """Returns the file that marks a session's current reply as editing."""
    safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return pathlib.Path(tempfile.gettempdir()) / f"clickup-edit-{safe_id}"


def session_start() -> None:
    """Adds the instruction to list the ClickUp tasks to Claude's context."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": _SESSION_START_CONTEXT,
                }
            }
        )
    )


def mark_edit(hook_input: dict) -> None:
    """Records that the current reply edited a file.

    Args:
        hook_input: The PostToolUse hook input.
    """
    _marker_path(hook_input.get("session_id", "")).touch()


def stop(hook_input: dict) -> None:
    """Asks Claude to update the tasks if the reply that ended edited files.

    Claude is asked at most once per reply: the marker is consumed, and a stop
    caused by this hook's own request is let through.

    Args:
        hook_input: The Stop hook input.
    """
    marker = _marker_path(hook_input.get("session_id", ""))
    if hook_input.get("stop_hook_active") or not marker.exists():
        return
    marker.unlink(missing_ok=True)
    print(json.dumps({"decision": "block", "reason": _STOP_REASON}))


def main() -> None:
    """Dispatches to the mode named on the command line."""
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    raw_input = sys.stdin.read()
    hook_input = json.loads(raw_input) if raw_input.strip() else {}
    if mode == "session-start":
        session_start()
    elif mode == "mark-edit":
        mark_edit(hook_input)
    elif mode == "stop":
        stop(hook_input)
    else:
        sys.exit(f"unknown mode: {mode!r}")


if __name__ == "__main__":
    main()
