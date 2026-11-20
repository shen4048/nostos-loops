#!/usr/bin/env python3
"""One-shot triage-record sale for Kunkun No.1 (machine 3224), GitHub Actions.

Mirrors the local 6h sale rule (set by the user 2026-11-16):
  - read the world day from the market view's first line
  - if that world day was already sold, do nothing
  - otherwise sell up to 24 triage records (all of them if fewer)
  - wilderness rejection (no market here) -> do not mark the day sold

State lives in sale_state.json next to this script; the workflow commits
it back after a run that changed it.

Env:
  GALATEA_TOKEN  the account API key (stored as a GitHub repo secret)
  SALE_SHADOW    "1" = read-only: print what would be sold, never act

Exit codes: 0 = fine (incl. nothing due / shadow / wilderness).
            2 = something needs a human look.
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
ITEM = "triage_record"
CAP = 24
SHADOW = os.environ.get("SALE_SHADOW", "1") == "1"
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "sale_state.json")


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
                          "request_id": f"ghsale-{tag}-{int(time.time())}"})


def parse_item(text: str, item_id: str) -> float | None:
    m = re.search(re.escape(item_id) + r"\](?:约)?(\d+(?:\.\d+)?)(半)?", text)
    if not m:
        return None
    val = float(m.group(1))
    if m.group(2):
        val += 0.5
    return val


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_sale_world_day": 0}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main() -> int:
    token = os.environ.get("GALATEA_TOKEN", "").strip()
    if not token:
        print("GALATEA_TOKEN is not set")
        return 2
    mcp = Mcp(token)

    market = mcp.call("nostos_status", {"view": "market"})
    if market is None:
        print("read market view failed")
        return 2
    m = re.search(r"世界第\s*(\d+)\s*天", market)
    if not m:
        print(f"cannot parse world day from market view: {market[:200]}")
        return 2
    world_day = int(m.group(1))

    state = load_state()
    last = int(state.get("last_sale_world_day", 0))
    print(f"world day {world_day}, last sale world day {last}, shadow={SHADOW}")
    if world_day <= last:
        print("already sold for this world day, nothing to do")
        return 0

    inv = mcp.call("nostos_status", {"view": "inventory"})
    if inv is None:
        print("read inventory failed")
        return 2
    count = parse_item(inv, ITEM)
    if count is None:
        print("cannot parse triage_record count from inventory")
        return 2
    print(f"triage records in bag: {count}")

    if count < 1:
        print("sold out: no triage records to sell")
        if not SHADOW:
            state["last_sale_world_day"] = world_day
            state["records_after"] = 0
            save_state(state)
        return 0

    qty = min(CAP, int(count))
    if SHADOW:
        print(f"shadow mode: would sell {qty} triage records")
        return 0

    r = mcp.act({"id": "sell", "target": ITEM, "quantity": qty}, "sell")
    if r is None:
        print("sell returned nothing")
        return 2
    if "荒野" in r or "没有凭空出现的买家" in r:
        print(f"sell refused (no market here), day NOT marked sold: {r[:200]}")
        return 0

    # The sell reply itself confirms the trade ("...交到当地人手里，换回N德").
    amt = re.search(r"换回\s*(\d+(?:\.\d+)?)\s*德", r)
    inv2 = mcp.call("nostos_status", {"view": "inventory"})
    after = None
    if inv2 is not None:
        after = parse_item(inv2, ITEM)
        if after is None and ITEM not in inv2:
            after = 0.0  # sold out: the item line disappears at zero
    if amt is None and (after is None or after > count - qty + 0.01):
        print(f"sell outcome unclear (before={count}, after={after}): {r[:300]}")
        return 2
    if after is None:
        after = count - qty

    print(f"sold {qty} triage records"
          + (f" for {amt.group(1)}" if amt else "")
          + f"; records left: {after}")
    state["last_sale_world_day"] = world_day
    state["records_after"] = after
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
