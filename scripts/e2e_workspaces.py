"""Workspaces e2e: a Sports-scoped chat only ever sees and calls Sports tools.

Uses the real "Sports" workspace (the 7 BBC Sport workflows; created if missing and kept),
plus a TEMPORARY second workspace with TEMPORARY workflows (books.toscrape / quotes.toscrape)
that are always deleted at the end, so only Sports and the BBC workflows remain.

Checks: /api/skills, /api/skills/search, /mcp/{workspace} tools/list + tools/call,
/api/catalog?workspace=, /api/catalog/workspaces, and two real chats (Sports with a
deliberately cross-domain request, and the temp workspace). Never prints tokens or keys.

  BROWSER_PLATFORM_BACKEND=http://127.0.0.1:8899 python scripts/e2e_workspaces.py --out report.json
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import httpx

BACKEND = os.environ.get("BROWSER_PLATFORM_BACKEND", "https://browser.129-159-156-75.sslip.io")
BBC = [
    "9f27bcc839ee4cfb97d2865484addaf9",
    "fec9514f05c64aba9cc6f2c658dd42e0",
    "d79e4ca1c91940cb961579fc8d59df1c",
    "424b5b4fda854d0fb99c3f1a23084f06",
    "fd896b7078cb45b7bc4644f758941f04",
    "ca3a55ebce5d4ebcbca70f31cff2a9d6",
    "62818d1476b349d5b9b556b080a766a0",
]
SPORTS = {
    "name": "Sports",
    "slug": "sports",
    "description": "BBC Sport: football fixtures, results, league tables and top scorers; cricket, rugby union and Formula 1 results.",
    "instructions": "You are the BBC Sport assistant. Use only these BBC Sport tools; answer from their results and say plainly when something is outside BBC Sport.",
}
TEMP_WS = {"name": "E2E temp books", "description": "Temporary e2e workspace (books and quotes practice sites)."}
TEMP_WFS = [
    {
        "name": "e2e_tmp_books_category",
        "description": "Book titles and prices in the Travel category of books.toscrape.com",
        "version": "1",
        "steps": [
            {"type": "navigation", "url": "https://books.toscrape.com/catalogue/category/books/travel_2/index.html"},
            {"type": "extract", "extractionGoal": "book titles and prices", "output": "extract"},
        ],
    },
    {
        "name": "e2e_tmp_quotes_tag",
        "description": "Quotes and authors for a tag on quotes.toscrape.com",
        "version": "1",
        "input_schema": [{"name": "tag", "type": "string", "required": True, "default": "love"}],
        "steps": [
            {"type": "navigation", "url": "https://quotes.toscrape.com/tag/{tag}/"},
            {"type": "extract", "extractionGoal": "quotes and their authors", "output": "extract"},
        ],
    },
]


def token() -> str:
    value = os.environ.get("BROWSER_PLATFORM_USER_TOKEN") or os.environ.get("BROWSER_PLATFORM_ACCESS_TOKEN") or ""
    if not value:
        raise SystemExit("access token is not set")
    return value


def check(results: list, name: str, ok: bool, detail: object = "") -> None:
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + (f"  {detail}" if detail and not ok else ""), flush=True)


def mcp(client: httpx.Client, path: str, method: str, params: dict | None = None) -> dict:
    return client.post(f"{BACKEND}{path}", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).json()


def chat(client: httpx.Client, message: str, workspace: str) -> dict:
    started = time.time()
    job = client.post(f"{BACKEND}/api/chat", json={"message": message, "target": "cloud", "stream": False, "confirm": True, "workspace": workspace}).json()
    job["_seconds"] = round(time.time() - started, 1)
    return job


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    results: list = []
    report: dict = {"backend": BACKEND, "chats": {}}
    temp_ws_id = None
    temp_wf_ids: list[str] = []
    with httpx.Client(timeout=httpx.Timeout(400, connect=30), headers={"X-Browser-Platform-Token": token()}) as client:
        library = {w["id"] for w in client.get(f"{BACKEND}/api/workflows").json()["workflows"]}
        missing = [i for i in BBC if i not in library]
        if missing:
            raise SystemExit(f"BBC workflows missing: {missing}")
        sports = client.get(f"{BACKEND}/api/workspaces/sports")
        if sports.status_code == 404:
            sports = client.post(f"{BACKEND}/api/workspaces", json={**SPORTS, "workflow_ids": BBC})
        sports = sports.json()
        if set(sports["workflow_ids"]) != set(BBC):
            sports = client.patch(f"{BACKEND}/api/workspaces/{sports['id']}", json={"workflow_ids": BBC}).json()
        check(results, "sports workspace holds exactly the 7 BBC workflows", set(sports["workflow_ids"]) == set(BBC))
        try:
            ws = client.post(f"{BACKEND}/api/workspaces", json=TEMP_WS).json()
            temp_ws_id = ws["id"]
            for wf in TEMP_WFS:
                created = client.post(f"{BACKEND}/api/workflows", json={**wf, "workspace_ids": [temp_ws_id]}).json()
                temp_wf_ids.append(created["id"])
                check(results, f"created {wf['name']} straight into temp workspace", created.get("workspaces") == [ws["slug"]], created.get("workspaces"))
            tslug = ws["slug"]

            skills = client.get(f"{BACKEND}/api/skills", params={"workspace": "sports"}).json()["skills"]
            check(results, "/api/skills?workspace=sports lists only BBC skills", {s["id"] for s in skills} <= set(BBC) and len(skills) >= 1, [s["slug"] for s in skills])
            found = client.get(f"{BACKEND}/api/skills/search", params={"q": "travel book prices and love quotes", "workspace": "sports"}).json()["skills"]
            check(results, "search in sports never returns temp skills", not ({s["id"] for s in found} & set(temp_wf_ids)), [s["slug"] for s in found])
            listed = mcp(client, "/mcp/sports", "tools/list")["result"]["tools"]
            names = [t["name"] for t in listed]
            check(results, "/mcp/sports tools/list = BBC tools only", not any(n.startswith("e2e_tmp") for n in names) and len(names) >= 1, names)
            tlisted = [t["name"] for t in mcp(client, f"/mcp/{tslug}", "tools/list")["result"]["tools"]]
            check(results, f"/mcp/{tslug} lists exactly the 2 temp tools", sorted(tlisted) == ["e2e_tmp_books_category", "e2e_tmp_quotes_tag"], tlisted)
            call = mcp(client, "/mcp/sports", "tools/call", {"name": "e2e_tmp_quotes_tag", "arguments": {"tag": "love"}})
            check(results, "/mcp/sports refuses a tool from another workspace", "Unknown tool" in json.dumps(call.get("error") or {}), call.get("error"))
            whole = [t["name"] for t in mcp(client, "/mcp", "tools/list", {"limit": 50})["result"]["tools"]]
            check(results, "/mcp (no workspace) still lists the whole library", "e2e_tmp_books_category" in whole and len(whole) >= len(names) + 2)
            cat = httpx.get(f"{BACKEND}/api/catalog", params={"workspace": "sports"}).json()["workflows"]
            check(results, "public /api/catalog?workspace=sports has no temp workflows", not ({w["id"] for w in cat} & set(temp_wf_ids)))
            pub = httpx.get(f"{BACKEND}/api/catalog/workspaces").json()["workspaces"]
            check(results, "public /api/catalog/workspaces lists sports without instructions", any(w["slug"] == "sports" for w in pub) and all("instructions" not in w for w in pub))

            # Chat 1: Sports, deliberately asking for something only the temp workspace could do.
            msg1 = "What are Arsenal's next fixtures, and what do travel books cost on books.toscrape?"
            job = chat(client, msg1, "sports")
            report["chats"]["sports_cross_domain"] = {k: job.get(k) for k in ("status", "workspace", "skills_considered", "calls", "answer", "uncovered", "usage", "_seconds", "error")}
            called = {c.get("workflow_id") for c in job.get("calls") or []}
            check(results, "sports chat: job scoped to sports", (job.get("workspace") or {}).get("slug") == "sports", job.get("workspace"))
            check(results, "sports chat: no temp skill ever offered to the planner", not any(str(s).startswith("e2e_tmp") for s in job.get("skills_considered") or []), job.get("skills_considered"))
            check(results, "sports chat: every call is a BBC workflow", called and called <= set(BBC), sorted(called))
            check(results, "sports chat: fixtures were called", "9f27bcc839ee4cfb97d2865484addaf9" in called)
            check(results, "sports chat: finished (succeeded/partial)", job.get("status") in {"succeeded", "partial"}, job.get("status"))

            # Chat 2: the temp workspace can do books and never sees BBC tools.
            job2 = chat(client, "Give me two love quotes and their authors from quotes.toscrape", tslug)
            report["chats"]["temp_quotes"] = {k: job2.get(k) for k in ("status", "workspace", "skills_considered", "calls", "answer", "usage", "_seconds", "error")}
            called2 = {c.get("workflow_id") for c in job2.get("calls") or []}
            check(results, "temp chat: only temp workflows called", called2 and called2 <= set(temp_wf_ids), sorted(called2))
            check(results, "temp chat: no BBC skill offered", not (set(job2.get("skills_considered") or []) & {s["slug"] for s in skills}), job2.get("skills_considered"))
            check(results, "temp chat: succeeded", job2.get("status") == "succeeded", job2.get("status"))
        finally:
            for wf_id in temp_wf_ids:
                client.delete(f"{BACKEND}/api/workflows/{wf_id}")
            if temp_ws_id:
                client.delete(f"{BACKEND}/api/workspaces/{temp_ws_id}")
            left_wf = {w["id"] for w in client.get(f"{BACKEND}/api/workflows").json()["workflows"]}
            left_ws = [w["slug"] for w in client.get(f"{BACKEND}/api/workspaces").json()["workspaces"]]
            check(results, "cleanup: temp workflows deleted", not (left_wf & set(temp_wf_ids)))
            check(results, "cleanup: temp workspace deleted", temp_ws_id is None or all(not s.startswith("e2e-temp") for s in left_ws), left_ws)
            report["after"] = {"workflows": len(left_wf), "workspaces": left_ws, "bbc_present": set(BBC) <= left_wf}
    report["results"] = results
    report["passed"] = sum(r["ok"] for r in results)
    report["total"] = len(results)
    print(f"{report['passed']}/{report['total']} checks passed", flush=True)
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
