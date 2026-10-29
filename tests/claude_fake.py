"""Fake `claude` executable for tests: logs its invocation, then replays a scripted stream-json run.

FAKE_CLAUDE_SCRIPT is a JSON list. Plain objects are printed as stream-json lines; directives:
{"sleep": seconds}, {"write": {"path": relative path, "text": ...}}, {"spawn_sleep": seconds}
(a child process in the same process group, its pid logged as "child").
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def fake_claude(tmp, script, *, exit_code=0):
    """Return (executable, log path, environment to patch in)."""
    tmp = Path(tmp)
    executable = tmp / "claude"
    executable.write_text(
        f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        "import claude_fake\nclaude_fake.main()\n"
    )
    executable.chmod(0o755)
    log = tmp / "claude-log.jsonl"
    env = {
        "FAKE_CLAUDE_LOG": str(log),
        "FAKE_CLAUDE_SCRIPT": json.dumps(script),
        "FAKE_CLAUDE_EXIT": str(exit_code),
    }
    return str(executable), log, env


def read_log(log):
    return [json.loads(line) for line in Path(log).read_text().splitlines()]


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _log(entry):
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as stream:
        stream.write(json.dumps(entry) + "\n")


def main():
    stdin = sys.stdin.read()
    _log({"argv": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd(),
          "env": sorted(os.environ), "pid": os.getpid()})
    for item in json.loads(os.environ["FAKE_CLAUDE_SCRIPT"]):
        if "sleep" in item:
            time.sleep(item["sleep"])
        elif "write" in item:
            Path(item["write"]["path"]).write_text(item["write"]["text"])
        elif "spawn_sleep" in item:
            child = subprocess.Popen(["sleep", str(item["spawn_sleep"])])
            _log({"child": child.pid})
        else:
            print(json.dumps(item), flush=True)
    sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
