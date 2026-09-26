"""Record the retail smoke conversation against a real model: every SSE event and each
turn's dynamic context, with tool-use and session ids replaced by placeholders.

    env -u ANTHROPIC_BASE_URL .venv/bin/python traces/record_retail.py traces/out.json [--fourth-turn]

Runs scripts/smoke_chat.py in-process without editing it. Per-round cache numbers are in
the orchestrator's INFO log lines, not in the saved file.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FOURTH_TURN = {
    "message": "What else should we pack for a first family camping trip?",
    "expect_tools": set(),
    "expect_events": {"turn_complete"},
}


def load_smoke():
    spec = importlib.util.spec_from_file_location("smoke_chat", REPO / "scripts" / "smoke_chat.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    return smoke


def scrub(text: str) -> str:
    tool_ids: dict[str, str] = {}
    sessions: dict[str, str] = {}

    def tool_id(match: re.Match[str]) -> str:
        return tool_ids.setdefault(match.group(0), f"toolu_{len(tool_ids) + 1:03d}")

    def session_id(match: re.Match[str]) -> str:
        return sessions.setdefault(match.group(0), f"session-{chr(65 + len(sessions))}")

    text = re.sub(r"toolu_[A-Za-z0-9]+", tool_id, text)
    return re.sub(r"\b[0-9a-f]{12}\b", session_id, text)


def main() -> int:
    out = Path(sys.argv[1])
    smoke = load_smoke()
    # Imported after smoke_chat, which puts examples/ on sys.path.
    import shopping_agent_runtime.orchestrator as orchestrator

    contexts: list[str] = []
    build_dynamic_context = orchestrator.build_dynamic_context

    def recording_build(**kwargs):
        text = build_dynamic_context(**kwargs)
        contexts.append(text)
        return text

    orchestrator.build_dynamic_context = recording_build

    if "--fourth-turn" in sys.argv:
        smoke.VERTICAL_TURNS["retail"].append(FOURTH_TURN)

    turns: list[dict] = []
    run_turn = smoke.run_turn

    async def recording_run_turn(client, headers, message, *, merchant):
        events = await run_turn(client, headers, message, merchant=merchant)
        turns.append({"message": message, "context": contexts[-1], "events": events})
        out.write_text(scrub(json.dumps(turns, ensure_ascii=False, indent=1)))
        return events

    smoke.run_turn = recording_run_turn
    sys.argv = ["smoke_chat.py", "--vertical", "retail"]
    code = smoke.main()
    print("trace saved:", out)
    return code


if __name__ == "__main__":
    sys.exit(main())
