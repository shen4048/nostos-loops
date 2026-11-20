#!/usr/bin/env python3
"""One-shot triage tick for Kunkun No.1 (machine 3224), for GitHub Actions.

Each run does exactly one daemon tick and exits:
  busy (a run / travel / sleep in progress) -> exit 0
  idle -> guards -> sleep / drink / eat / start one triage round

Env:
  GALATEA_TOKEN  the account API key (stored as a GitHub repo secret)
  TICK_SHADOW    "1" = read-only: print what would be done, never act

Exit codes: 0 = fine (incl. busy / shadow). 2 = guard stop or start
refused, so the Actions run shows red and a human looks.
Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

MCP_URL = "https://galatea.abysslumina.com/mcp"
PROTOCOL_VERSION = "2025-06-18"
PROCESS = "medicine_field_triage"
SHADOW = os.environ.get("TICK_SHADOW", "1") == "1"


class Mcp:
    def __init__(self, token: str):
        self.token = token
        self.session = None
        self._id = 0
        self._post({"jsonrpc": "2.0", "id": self._next(), "method": "initialize",
                    "params": {"protocolVersion": PROTOCOL_VERSION,
                               "capabilities": {},
                               "clientInfo": {"name": "gh-tick", "version": "1"}}},
                   expect_reply=True)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                   expect_reply=False)

    def _next(self) -> int:
        self._id += 1
        return self._id

    def _post(self, payload: dict, expect_reply: bool):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(MCP_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("Authorization", f"Bearer {self.token}")
        if self.session:
            req.add_header("Mcp-Session-Id", self.session)
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise SystemExit(f"HTTP {exc.code} from MCP server: {body[:500]}")
        self.session = resp.headers.get("Mcp-Session-Id") or self.session
        if not expect_reply:
            return None
        raw = resp.read().decode("utf-8", "replace")
        messages = []
        if "text/event-stream" in (resp.headers.get("Content-Type") or ""):
            for line in raw.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if chunk and chunk != "[DONE]":
                        messages.append(json.loads(chunk))
        elif raw.strip():
            parsed = json.loads(raw)
            messages = parsed if isinstance(parsed, list) else [parsed]
        for msg in messages:
            if isinstance(msg, dict) and msg.get("id") == payload.get("id"):
                if "error" in msg:
                    raise SystemExit(f"MCP error: {msg['error']}")
                return msg.get("result")
        return messages[-1] if messages else None

    def call(self, tool: str, args: dict) -> str | None:
        result = self._post({"jsonrpc": "2.0", "id": self._next(),
                             "method": "tools/call",
                             "params": {"name": tool, "arguments": args}},
                            expect_reply=True)
        try:
            return result["content"][0]["text"]
        except Exception:
            print(f"unparsable result from {tool}: {str(result)[:300]}")
            return None

    def act(self, command: dict, tag: str) -> str | None:
        return self.call("nostos_act",
                         {"command": command,
                          "request_id": f"ghtick-{tag}-{int(time.time())}"})


def parse_vitals(text: str) -> dict | None:
    m = re.search(r"健康(\d+)、精力(\d+)、水分(\d+)、饱腹(\d+)、体温(\d+)", text)
    if not m:
        return None
    keys = ["health", "energy", "water", "food", "temp"]
    return {k: int(v) for k, v in zip(keys, m.groups())}


def parse_item(text: str, item_id: str) -> float | None:
    m = re.search(re.escape(item_id) + r"\](?:约)?(\d+(?:\.\d+)?)(半)?", text)
    if not m:
        return None
    val = float(m.group(1))
    if m.group(2):
        val += 0.5
    return val


def main() -> int:
    token = os.environ.get("GALATEA_TOKEN", "").strip()
    if not token:
        print("GALATEA_TOKEN is not set")
        return 2
    mcp = Mcp(token)

    work = mcp.call("nostos_status", {"view": "work"})
    if work is None:
        print("read work view failed")
        return 2
    busy_run = re.search(r"run:(\d+)", work)
    idle = "没有正在进行" in work
    if busy_run and not idle:
        print(f"busy: run:{busy_run.group(1)} in progress, nothing to do")
        return 0
    if not idle and not busy_run:
        print("busy: travelling / sleeping / other activity, nothing to do")
        return 0

    status = mcp.call("nostos_status", {"view": "status"})
    if status is None:
        print("read status failed")
        return 2
    vitals = parse_vitals(status)
    if vitals is None:
        print(f"cannot parse vitals from status: {status[:200]}")
        return 2
    print(f"vitals: {vitals} shadow={SHADOW}")

    if min(vitals.values()) < 36:
        print(f"GUARD STOP: a vital is below 36: {vitals}")
        return 2

    debt = ("睡眠债" in status) or ("连续清醒" in status and "损伤" in status)
    if debt or vitals["energy"] < 70:
        print(f"guard: sleep 1h (energy={vitals['energy']}, debt={debt})")
        if not SHADOW:
            mcp.act({"id": "sleep", "target": "1h"}, "sleep")
        return 0

    if vitals["water"] < 60 or vitals["food"] < 60:
        if vitals["water"] < 60:
            n = max(1, round((90 - vitals["water"]) / 17))
            print(f"guard: drink x{n} (water={vitals['water']})")
            if not SHADOW:
                mcp.act({"id": "drink", "target": "potable_water", "quantity": n}, "drink")
        if vitals["food"] < 60:
            n = max(1, round((85 - vitals["food"]) / 22))
            print(f"guard: eat x{n} (food={vitals['food']})")
            if not SHADOW:
                mcp.act({"id": "eat", "target": "edible_forage", "quantity": n}, "eat")
        return 0

    inv = mcp.call("nostos_status", {"view": "inventory"})
    if inv is None:
        print("read inventory failed")
        return 2
    water_bottles = parse_item(inv, "potable_water")
    print(f"drinking water in bag: {water_bottles}")
    if water_bottles is None or water_bottles < 1:
        print("GUARD STOP: no drinking water left for triage")
        return 2

    print(f"action: start {PROCESS}")
    if SHADOW:
        print("shadow mode: not started")
        return 0
    r = mcp.act({"id": "start", "target": PROCESS}, "triage")
    m = re.search(r"run:(\d+)", r) if r else None
    if m is None:
        print(f"START REFUSED: {(r or '')[:300]}")
        return 2
    print(f"started run:{m.group(1)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
