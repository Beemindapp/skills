

import argparse
import glob
import importlib.util
import json
import os
import random
import statistics

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("timeplan", os.path.join(HERE, "timeplan.py"))
tp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tp)

def turns_from(path):
    out = []
    apis = {}
    cur, req, last = None, None, None

    def finish_req():
        nonlocal req
        if req and req["start"] and req["end"]:
            cur["out"] += req["out"]
        req = None

    def flush():
        if cur and cur["req"] and cur["t0"] and cur["t1"]:
            wall = cur["t1"] - cur["t0"]
            if 0 < wall < 3600:
                out.append(cur)

    for line in open(path, errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        t, msg, tsx = d.get("type"), d.get("message") or {}, d.get("timestamp")
        if t == "api-request":
            apis[d.get("id")] = tp._ts(tsx) if tsx else None
            continue
        if t == "assistant" and tsx:
            mid = msg.get("id") or d.get("uuid")
            tsf = tp._ts(tsx)
            if cur is None:
                cur = {"req": 0, "out": 0, "tools": {}, "t0": tsf, "t1": None, "model": None}
            if req is None or mid != req["id"]:
                finish_req()
                cur["req"] += 1
                req = {"id": mid, "start": apis.get(d.get("requestRef")) or last,
                       "end": None, "out": 0}
            req["end"] = tsf
            req["out"] = max(req["out"], (msg.get("usage") or {}).get("output_tokens") or 0)
            cur["t1"] = tsf
            cur["model"] = d.get("requestedModel") or msg.get("model")
            for blk in (msg.get("content") or []):
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    cur["tools"][blk.get("name")] = cur["tools"].get(blk.get("name"), 0) + 1
            last = tsf
        elif t == "user" and tsx:
            finish_req()
            c = msg.get("content")
            if isinstance(c, str) and c.strip():
                flush()
                cur = {"req": 0, "out": 0, "tools": {}, "t0": tp._ts(tsx), "t1": None,
                       "model": None}
            last = tp._ts(tsx)
    finish_req()
    flush()
    return [r for r in out if r["model"]]

def turns_from_pi(path):
    out, cur = [], None

    def flush():
        if cur and cur["req"] and cur["t0"] is not None and cur["t1"] is not None:
            wall = cur["t1"] - cur["t0"]
            if 0 < wall < 3600:
                out.append(cur)

    for line in open(path, errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "message":
            continue
        m = d.get("message") or {}
        role = m.get("role")
        ts = tp._pi_ts(m, d)
        if role == "user":
            flush()
            cur = {"req": 0, "out": 0, "tools": {}, "t0": ts, "t1": None, "model": None}
        elif role == "assistant":
            if cur is None:
                cur = {"req": 0, "out": 0, "tools": {}, "t0": ts, "t1": None, "model": None}
            cur["req"] += 1
            cur["out"] += (m.get("usage") or {}).get("output") or 0
            cur["t1"] = ts + (m.get("durationMs") or 0) / 1000.0
            cur["model"] = (m.get("model") or "unknown").split("/")[-1]
            for blk in (m.get("content") or []):
                if isinstance(blk, dict) and blk.get("type") in ("toolCall", "tool_call"):
                    cur["tools"][blk.get("name")] = cur["tools"].get(blk.get("name"), 0) + 1
    flush()
    return [r for r in out if r["model"]]

def predict(rates, model_key, tokens, tools):
    m = rates["models"][model_key]
    n_req = max(1, args_requests(tools, rates))
    sec = (m["first_req_s"] + max(n_req - 1, 0) * m["next_req_s"]
           + tokens / max(m["decode_tok_s"], 1))
    sec *= m.get("turn_scale", 1.0)
    for name, cnt in tools.items():
        real, tv = tp.tool_lookup(rates, name)
        sec += cnt * (tv["med_s"] if tv else 1.0)
    return sec

def args_requests(tools, rates):
    n = sum(tools.values())
    return max(1, round(n * rates["turn"].get("req_per_tool", 1.0))) if n else 1

def main():
    global args_requests
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=400)
    ap.add_argument("--files", type=int, default=150)
    a = ap.parse_args()

    rates = json.load(open(os.path.join(HERE, "data", "rates.json")))
    paths = []
    for _name, root in tp.sources("all"):
        paths += glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    paths = sorted(paths)
    random.seed(7)
    rows = []
    for p in random.sample(paths, min(a.files, len(paths))):
        if not p.endswith(".jsonl"):
            continue
        reader = turns_from_pi if "/pi/sessions/" in p else turns_from
        for t in reader(p):
            if t["model"] in rates["models"] and t["out"] > 0:
                rows.append(t)
    random.shuffle(rows)
    rows = [r for r in rows if 0 < (r["t1"] - r["t0"]) < 3600][: a.turns]

    ratios, by_model = [], {}
    for r in rows:
        pred = predict(rates, r["model"], r["out"], r["tools"])
        if pred > 0:
            ratios.append(pred / (r["t1"] - r["t0"]))
            by_model.setdefault(r["model"], []).append(pred / (r["t1"] - r["t0"]))
    ratios.sort()
    n = len(ratios)
    print(f"turns tested: {n}")
    if not n:
        return
    print(f"predicted/actual  p10 {ratios[int(.1*n)]:.2f} · median {statistics.median(ratios):.2f}"
          f" · p90 {ratios[int(.9*n)]:.2f}")
    print(f"within 2×: {sum(1 for x in ratios if .5 <= x <= 2)/n*100:.0f}%   "
          f"within 3×: {sum(1 for x in ratios if 1/3 <= x <= 3)/n*100:.0f}%")
    print("1.00 = perfect. Under 1 means the estimate is optimistic.")
    print()
    print(f"{'model':28s} {'turns':>6s} {'median':>7s} {'within 2x':>10s}")
    for name, v in sorted(by_model.items(), key=lambda kv: -len(kv[1]))[:8]:
        if len(v) < 10:
            continue
        v.sort()
        print(f"{name:28s} {len(v):6d} {statistics.median(v):7.2f} "
              f"{sum(1 for x in v if .5 <= x <= 2)/len(v)*100:9.0f}%")

if __name__ == "__main__":
    main()
