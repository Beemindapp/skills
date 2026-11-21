

import argparse
import datetime as dt
import glob
import json
import math
import os
import random
import statistics
import fcntl
import sys
import zlib

DESC = """How long agent work really takes, measured on this machine.

  calibrate            rescan transcripts into data/rates.json (incremental)
  show                 the measured table: tok/s, tokens/hour, tool latency, turns
  estimate             time one piece of work
  schedule             time a multi-step plan from a JSON file
  done                 record what really happened, so estimates self-correct
  quick                regenerate QUICK.md from rates.json
"""

HERE = os.path.dirname(os.path.realpath(__file__))
DATA = os.path.join(HERE, "data")
RATES = os.path.join(DATA, "rates.json")
CACHE = os.path.join(DATA, "files.jsonl")
HISTORY = os.path.join(HERE, "history.tsv")
LESSONS = os.path.join(HERE, "lessons.md")
CLAUDE_ROOT = os.path.expanduser(os.environ.get("TIMEPLAN_CLAUDE_ROOT", "~/.claude/projects"))


def first_dir(*paths):
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            return p
    return os.path.expanduser(paths[0])


PI_ROOT = first_dir(os.environ.get("TIMEPLAN_PI_ROOT") or "~/.pi/agent/sessions",
                    "~/.local/share/pi/sessions", "/opt/vibevoice/data/pi/sessions")

def sources(only=None):
    out = []
    for name, root in (("claude", CLAUDE_ROOT), ("pi", PI_ROOT)):
        if only and only != "all" and only != name:
            continue
        if os.path.isdir(root):
            out.append((name, root))
    return out

SEC_LO, SEC_HI, SEC_PER_DEC = 1e-2, 1e4, 20
RATE_LO, RATE_HI, RATE_PER_DEC = 0.1, 1e3, 20
TOK_LO, TOK_HI, TOK_PER_DEC = 1, 1e6, 10
SMALL_OUT = 200
BIG_OUT = 800
SAMPLE_K = 25
WALL_CAP_S = 1800
MAX_DUR_S = 4 * 3600

class Hist:

    def __init__(self, lo, hi, per_dec):
        self.lo, self.hi, self.per_dec = lo, hi, per_dec
        self.nb = int(round((math.log10(hi) - math.log10(lo)) * per_dec))
        self.b = {}

    def add(self, v):
        if v is None or v <= 0:
            return
        i = int((math.log10(v) - math.log10(self.lo)) * self.per_dec)
        i = min(max(i, 0), self.nb)
        self.b[i] = self.b.get(i, 0) + 1

    def merge(self, other):
        for k, v in (other or {}).items():
            k = int(k)
            self.b[k] = self.b.get(k, 0) + v

    def total(self):
        return sum(self.b.values())

    def pct(self, q):
        n = self.total()
        if not n:
            return None
        target, seen = q * n, 0
        for k in sorted(self.b):
            seen += self.b[k]
            if seen >= target:
                v = 10 ** (math.log10(self.lo) + (k + 0.5) / self.per_dec)
                return min(max(v, self.lo), self.hi)
        return self.hi

    def dump(self):
        return {str(k): v for k, v in sorted(self.b.items()) if v}

def H(lo, hi, per_dec):
    return Hist(lo, hi, per_dec)

def hsec():
    return H(SEC_LO, SEC_HI, SEC_PER_DEC)

def hrate():
    return H(RATE_LO, RATE_HI, RATE_PER_DEC)

def htok():
    return H(TOK_LO, TOK_HI, TOK_PER_DEC)

def _ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def scan_file(path):
    models, tools = {}, {}
    oh, wh = htok(), H(1.0, SEC_HI, 10)
    turn = {"n": 0, "out": 0, "req": 0, "tools": 0, "wall": 0.0,
            "req_ft": 0, "tools_ft": 0}
    code = {"chars": 0, "lines": 0}
    rng = random.Random(zlib.crc32(path.encode()))
    samples, seen = [], 0

    apis, pending_tool = {}, {}
    last, prev_kind = None, None
    req = None
    cur = {"out": 0, "req": 0, "tools": 0, "t0": None, "t1": None, "model": None}

    def finish_req():
        nonlocal req
        if req is None:
            return
        if req["start"] and req["end"] and req["out"] > 0:
            dur = req["end"] - req["start"]
            if 0 < dur < WALL_CAP_S:
                m = models.get(req["model"])
                if m is None:
                    m = models[req["model"]] = {
                        "n": 0, "out": 0, "dur": 0.0, "lh": hsec(),
                        "sh0": hsec(), "so0": htok(), "sh1": hsec(), "so1": htok(),
                        "bn": 0, "bout": 0, "bdur": 0.0, "rh": hrate()}
                m["n"] += 1
                m["out"] += req["out"]
                m["dur"] += dur
                m["lh"].add(dur)
                if req["out"] < SMALL_OUT:
                    k = "1" if req["follow"] else "0"
                    m["sh" + k].add(dur)
                    m["so" + k].add(req["out"])
                elif req["out"] >= BIG_OUT:
                    m["bn"] += 1
                    m["bout"] += req["out"]
                    m["bdur"] += dur
                    m["rh"].add(req["out"] / dur)
                cur["out"] += req["out"]
        req = None

    def flush_turn():
        nonlocal seen
        if cur["req"] and cur["t0"] and cur["t1"]:
            wall = cur["t1"] - cur["t0"]
            if 0 < wall < MAX_DUR_S:
                seen += 1
                rec = [cur["model"] or "unknown", cur["req"], cur["out"], cur["tools"],
                       round(wall, 2)]
                if len(samples) < SAMPLE_K:
                    samples.append(rec)
                else:
                    j = rng.randrange(seen)
                    if j < SAMPLE_K:
                        samples[j] = rec
                turn["n"] += 1
                turn["out"] += cur["out"]
                turn["req"] += cur["req"]
                turn["tools"] += cur["tools"]
                turn["wall"] += wall
                if cur["tools"]:
                    turn["req_ft"] += cur["req"]
                    turn["tools_ft"] += cur["tools"]
                oh.add(max(cur["out"], 1))
                wh.add(wall)

    def new_turn(t0):
        nonlocal cur
        cur = {"out": 0, "req": 0, "tools": 0, "t0": t0, "t1": None, "model": None}

    try:
        fh = open(path, errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            t, tsx = d.get("type"), d.get("timestamp")
            msg = d.get("message") or {}

            if t == "api-request":
                apis[d.get("id")] = tsx
                continue

            if t == "assistant" and tsx:
                mid = msg.get("id") or d.get("uuid")
                u = msg.get("usage") or {}
                out = u.get("output_tokens") or 0
                model = d.get("requestedModel") or msg.get("model") or "unknown"
                tsf = _ts(tsx)
                if req is None or mid != req["id"]:
                    finish_req()
                    if cur["t0"] is None:
                        cur["t0"] = tsf
                    cur["req"] += 1
                    raw = apis.get(d.get("requestRef")) or last
                    req = {"id": mid, "start": _ts(raw) if raw else None,
                           "out": 0, "end": None, "model": model,
                           "follow": prev_kind == "tool_result"}
                req["end"] = tsf
                req["out"] = max(req["out"], out)
                cur["t1"] = tsf
                cur["model"] = model
                for blk in (msg.get("content") or []):
                    if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                        continue
                    cur["tools"] += 1
                    pending_tool[blk.get("id")] = (blk.get("name"), tsx)
                    if blk.get("name") in ("Write", "Edit", "MultiEdit"):
                        inp = (d.get("wireToolInputs") or {}).get(blk.get("id")) \
                            or blk.get("input") or {}
                        body = inp.get("content") or inp.get("new_string") or ""
                        if isinstance(body, str) and body:
                            code["chars"] += len(body)
                            code["lines"] += body.count("\n")
                last = tsx
                prev_kind = "assistant"
                continue

            if t == "user" and tsx:
                finish_req()
                c = msg.get("content")
                is_result = isinstance(c, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
                prev_kind = "tool_result" if is_result else "prompt"
                if is_result:
                    for blk in c:
                        if not isinstance(blk, dict) or blk.get("type") != "tool_result":
                            continue
                        p = pending_tool.pop(blk.get("tool_use_id"), None)
                        if not p or not p[1]:
                            continue
                        dur = _ts(tsx) - _ts(p[1])
                        if 0 <= dur < MAX_DUR_S:
                            tl = tools.get(p[0])
                            if tl is None:
                                tl = tools[p[0]] = {"n": 0, "sum": 0.0, "h": hsec()}
                            tl["n"] += 1
                            tl["sum"] += dur
                            tl["h"].add(max(dur, SEC_LO))
                elif isinstance(c, str) and c.strip():

                    flush_turn()
                    new_turn(_ts(tsx))
                last = tsx
    finish_req()
    flush_turn()

    st = os.stat(path)
    return {
        "p": path, "m": int(st.st_mtime), "s": st.st_size, "a": "claude",
        "models": {k: {"n": v["n"], "out": v["out"], "dur": v["dur"],
                       "sh0": v["sh0"].dump(), "so0": v["so0"].dump(),
                       "sh1": v["sh1"].dump(), "so1": v["so1"].dump(),
                       "lh": v["lh"].dump(), "bn": v["bn"], "bout": v["bout"],
                       "bdur": v["bdur"], "rh": v["rh"].dump()}
                   for k, v in models.items()},
        "tools": {k: {"n": v["n"], "sum": v["sum"], "h": v["h"].dump()}
                  for k, v in tools.items()},
        "turn": turn, "oh": oh.dump(), "wh": wh.dump(), "code": code,
        "ts": samples,
    }

def _pi_ts(m, d):
    t = m.get("timestamp") or d.get("timestamp")
    if isinstance(t, (int, float)):
        return t / 1000.0 if t > 1e11 else float(t)
    return _ts(t) if isinstance(t, str) else None

def scan_pi(path):
    models, tools = {}, {}
    oh, wh = htok(), H(1.0, SEC_HI, 10)
    turn = {"n": 0, "out": 0, "req": 0, "tools": 0, "wall": 0.0, "req_ft": 0, "tools_ft": 0}
    rng = random.Random(zlib.crc32(path.encode()))
    samples, seen = [], 0
    cur = {"out": 0, "req": 0, "tools": 0, "t0": None, "t1": None, "model": None}
    prev_role = None

    def flush_turn():
        nonlocal seen, cur
        if cur["req"] and cur["t0"] and cur["t1"]:
            wall = cur["t1"] - cur["t0"]
            if 0 < wall < MAX_DUR_S:
                seen += 1
                rec = [cur["model"] or "unknown", cur["req"], cur["out"], cur["tools"],
                       round(wall, 2)]
                if len(samples) < SAMPLE_K:
                    samples.append(rec)
                else:
                    j = rng.randrange(seen)
                    if j < SAMPLE_K:
                        samples[j] = rec
                turn["n"] += 1
                turn["out"] += cur["out"]
                turn["req"] += cur["req"]
                turn["tools"] += cur["tools"]
                turn["wall"] += wall
                if cur["tools"]:
                    turn["req_ft"] += cur["req"]
                    turn["tools_ft"] += cur["tools"]
                oh.add(max(cur["out"], 1))
                wh.add(wall)

    def new_turn(t0):
        nonlocal cur
        cur = {"out": 0, "req": 0, "tools": 0, "t0": t0, "t1": None, "model": None}

    try:
        fh = open(path, errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "message":
                continue
            m = d.get("message") or {}
            role = m.get("role")
            ts = _pi_ts(m, d)
            if role == "user":
                flush_turn()
                new_turn(ts)
            elif role == "assistant":
                u = m.get("usage") or {}
                out = u.get("output") or 0
                dur = (m.get("durationMs") or 0) / 1000.0
                model = (m.get("model") or "unknown").split("/")[-1]
                if 0 < dur < WALL_CAP_S and out > 0:
                    mm = models.get(model)
                    if mm is None:
                        mm = models[model] = {
                            "n": 0, "out": 0, "dur": 0.0, "lh": hsec(),
                            "sh0": hsec(), "so0": htok(), "sh1": hsec(), "so1": htok(),
                            "bn": 0, "bout": 0, "bdur": 0.0, "rh": hrate()}
                    mm["n"] += 1
                    mm["out"] += out
                    mm["dur"] += dur
                    mm["lh"].add(dur)
                    if out < SMALL_OUT:
                        k = "1" if prev_role == "toolResult" else "0"
                        mm["sh" + k].add(dur)
                        mm["so" + k].add(out)
                    elif out >= BIG_OUT:
                        mm["bn"] += 1
                        mm["bout"] += out
                        mm["bdur"] += dur
                        mm["rh"].add(out / dur)
                if cur["t0"] is None:
                    cur["t0"] = ts

                cur["t1"] = ts + dur
                cur["req"] += 1
                cur["out"] += out
                cur["model"] = model
                for blk in (m.get("content") or []):
                    if isinstance(blk, dict) and blk.get("type") in ("toolCall", "tool_call"):
                        cur["tools"] += 1
            elif role == "toolResult":
                name = m.get("toolName") or "tool"
                dur = (m.get("durationMs") or 0) / 1000.0
                if 0 <= dur < MAX_DUR_S:
                    tl = tools.get(name)
                    if tl is None:
                        tl = tools[name] = {"n": 0, "sum": 0.0, "h": hsec()}
                    tl["n"] += 1
                    tl["sum"] += dur
                    tl["h"].add(max(dur, SEC_LO))
            prev_role = role
    flush_turn()

    st = os.stat(path)
    return {
        "p": path, "m": int(st.st_mtime), "s": st.st_size, "a": "pi",
        "models": {k: {"n": v["n"], "out": v["out"], "dur": v["dur"],
                       "sh0": v["sh0"].dump(), "so0": v["so0"].dump(),
                       "sh1": v["sh1"].dump(), "so1": v["so1"].dump(),
                       "lh": v["lh"].dump(), "bn": v["bn"], "bout": v["bout"],
                       "bdur": v["bdur"], "rh": v["rh"].dump()}
                   for k, v in models.items()},
        "tools": {k: {"n": v["n"], "sum": v["sum"], "h": v["h"].dump()}
                  for k, v in tools.items()},
        "turn": turn, "oh": oh.dump(), "wh": wh.dump(), "code": {"chars": 0, "lines": 0},
        "ts": samples,
    }

def load_cache():
    cache = {}
    if os.path.exists(CACHE):
        with open(CACHE) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                cache[r["p"]] = r
    return cache

def calibrate(args):
    os.makedirs(DATA, exist_ok=True)
    cache = {} if args.full else load_cache()
    srcs = sources(getattr(args, "source", None))
    if not srcs:
        sys.exit("no transcript roots found (looked for ~/.claude/projects and pi sessions)")
    scanners = {"claude": scan_file, "pi": scan_pi}
    keep, read, failed = {}, {}, 0
    for name, root in srcs:
        paths = sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True))
        read[name] = 0
        for p in paths:
            try:
                st = os.stat(p)
            except OSError:
                continue
            old = cache.get(p)
            if old and old["m"] == int(st.st_mtime) and old["s"] == st.st_size \
                    and old.get("a") == name:
                keep[p] = old
                continue
            r = scanners[name](p)
            read[name] += 1
            if r:
                keep[p] = r
            else:
                failed += 1
    with open(CACHE, "w") as fh:
        for p in sorted(keep):
            fh.write(json.dumps(keep[p], separators=(",", ":")) + "\n")
    rates = merge(list(keep.values()))
    rates["generated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    rates["files"] = len(keep)
    rates["read_now"] = read
    rates["bias"] = compute_bias()
    with open(RATES, "w") as fh:
        json.dump(rates, fh, indent=1)
    if not args.quiet:
        detail = ", ".join(f"{k} {v} read now" for k, v in read.items())
        print(f"calibrated {len(keep)} transcripts ({detail}, {failed} unreadable) -> {RATES}")
        print_rates(rates)
    return rates

def merge(rows):
    models, tools = {}, {}
    oh, wh = htok(), H(1.0, SEC_HI, 10)
    turn = {"n": 0, "out": 0, "req": 0, "tools": 0, "wall": 0.0, "req_ft": 0, "tools_ft": 0}
    code = {"chars": 0, "lines": 0}
    for r in rows:
        for name, m in (r.get("models") or {}).items():
            a = models.get(name)
            if a is None:
                a = models[name] = {"n": 0, "out": 0, "dur": 0.0,
                                    "sh0": hsec(), "so0": htok(), "sh1": hsec(), "so1": htok(),
                                    "lh": hsec(), "bn": 0, "bout": 0, "bdur": 0.0, "rh": hrate()}
            for k in ("n", "out", "dur", "bn", "bout", "bdur"):
                a[k] += m[k]
            for k in ("sh0", "so0", "sh1", "so1", "lh", "rh"):
                a[k].merge(m.get(k))
        for name, t in (r.get("tools") or {}).items():
            name = canonical_tool(name)
            a = tools.get(name)
            if a is None:
                a = tools[name] = {"n": 0, "sum": 0.0, "h": hsec()}
            a["n"] += t["n"]
            a["sum"] += t["sum"]
            a["h"].merge(t.get("h"))
        for k in turn:
            turn[k] += (r.get("turn") or {}).get(k, 0)
        oh.merge(r.get("oh"))
        wh.merge(r.get("wh"))
        for k in code:
            code[k] += (r.get("code") or {}).get(k, 0)

    out = {"models": {}, "tools": {}, "turn": {}, "code": {}, "sources": {}}
    for r in rows:
        a = r.get("a") or "claude"
        src = out["sources"].setdefault(a, {"transcripts": 0, "requests": 0, "out_tokens": 0,
                                            "api_hours": 0.0, "turns": 0})
        src["transcripts"] += 1
        src["turns"] += (r.get("turn") or {}).get("n", 0)
        for m in (r.get("models") or {}).values():
            src["requests"] += m["n"]
            src["out_tokens"] += m["out"]
            src["api_hours"] += m["dur"] / 3600
    for src in out["sources"].values():
        src["api_hours"] = round(src["api_hours"], 1)
    for name, m in models.items():
        if m["n"] < 5:
            continue
        agg = m["out"] / m["dur"] if m["dur"] else 0.0

        decode = (m["bout"] / m["bdur"]) if m["bdur"] else agg

        def fit(sh, so, q):
            return max((sh.pct(q) or 0.5) - (so.pct(0.50) or 0) / max(decode, 1.0), 0.15)
        for _ in range(3):
            first = fit(m["sh0"], m["so0"], 0.50)
            follow = fit(m["sh1"], m["so1"], 0.50)
            if m["bn"] and m["bdur"] > m["bn"] * follow:
                decode = m["bout"] / (m["bdur"] - m["bn"] * follow)
        out["models"][name] = {
            "alias": alias_of(name),
            "agents": sorted({r.get("a") or "claude" for r in rows
                              if name in (r.get("models") or {})}),
            "requests": m["n"],
            "out_tokens": m["out"],
            "api_hours": round(m["dur"] / 3600, 1),
            "tok_s": round(agg, 1),
            "tok_per_hour": round(agg * 3600),
            "decode_tok_s": round(decode, 1),
            "decode_tok_s_p10": round(m["rh"].pct(0.10) or decode, 1),
            "decode_tok_s_p90": round(m["rh"].pct(0.90) or decode, 1),
            "first_req_s": round(first, 2),
            "first_req_s_p90": round(max(fit(m["sh0"], m["so0"], 0.90), first), 2),
            "next_req_s": round(follow, 2),
            "next_req_s_p90": round(max(fit(m["sh1"], m["so1"], 0.90), follow), 2),
            "latency_s_p50": round(m["lh"].pct(0.50) or 0, 2),
        }
    for name, t in tools.items():
        if t["n"] < 5:
            continue
        out["tools"][name] = {
            "calls": t["n"],
            "med_s": round(t["h"].pct(0.50) or 0, 2),
            "p90_s": round(t["h"].pct(0.90) or 0, 2),
            "hours": round(t["sum"] / 3600, 2),
        }
    if turn["n"]:
        out["turn"] = {
            "turns": turn["n"],
            "out_tokens_p50": round(oh.pct(0.50) or 0),
            "out_tokens_p90": round(oh.pct(0.90) or 0),
            "wall_s_p50": round(wh.pct(0.50) or 0, 1),
            "wall_s_p90": round(wh.pct(0.90) or 0, 1),
            "tok_per_turn": round(turn["out"] / turn["n"]),
            "req_per_turn": round(turn["req"] / turn["n"], 1),
            "tools_per_turn": round(turn["tools"] / turn["n"], 1),
            "wall_s_per_turn": round(turn["wall"] / turn["n"], 1),
            "req_per_tool": round(turn["req_ft"] / turn["tools_ft"], 2) if turn["tools_ft"] else 1.5,
        }

    samples = []
    for r in rows:
        samples.extend(r.get("ts") or [])
    tot_calls = sum(v["calls"] for v in out["tools"].values()) or 1
    tool_per_call = sum(v["calls"] * v["med_s"] for v in out["tools"].values()) / tot_calls
    out["tool_s_per_call"] = round(tool_per_call, 2)
    by_model = {}
    for m, req, tok, tools, wall in samples:
        if m in out["models"] and req >= 1 and wall >= 0.1:
            by_model.setdefault(m, []).append((req, tok, tools, wall))

    def predict_s(A, s, B):
        return s * A + B

    for name, rows_m in by_model.items():
        if len(rows_m) < 30:
            continue
        m = out["models"][name]
        A = [(m["first_req_s"] + max(req - 1, 0) * m["next_req_s"]
              + tok / max(m["decode_tok_s"], 1.0)) for req, tok, tools, wall in rows_m]
        B = [tools * tool_per_call for req, tok, tools, wall in rows_m]
        W = [wall for req, tok, tools, wall in rows_m]

        def err(s):
            return statistics.median([predict_s(a, s, b) - w for a, b, w in zip(A, B, W)])
        lo, hi = 0.05, 5.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if err(mid) > 0:
                hi = mid
            else:
                lo = mid
        scale = round((lo + hi) / 2, 3)
        ratios = sorted((predict_s(a, scale, b)) / w for a, b, w in zip(A, B, W))
        m["turn_scale"] = scale
        m["spread_p10"] = round(ratios[int(0.10 * len(ratios))], 2)
        m["spread_p90"] = round(ratios[min(int(0.90 * len(ratios)), len(ratios) - 1)], 2)
        m["fit_turns"] = len(ratios)

    if code["lines"]:
        out["code"] = {"line_tokens": round(code["chars"] / 4 / code["lines"], 1),
                       "lines": code["lines"]}
    out["total_out_tokens"] = sum(m["out_tokens"] for m in out["models"].values())
    out["total_api_hours"] = round(sum(v["api_hours"] for v in out["models"].values()), 1)
    return out

TOOL_NAMES = {"bash": "Bash", "read": "Read", "write": "Write", "edit": "Edit",
              "glob": "Glob", "grep": "Grep", "webfetch": "WebFetch", "websearch": "WebSearch",
              "list": "List", "ls": "Bash", "multiedit": "Edit", "patch": "Edit",
              "agent": "Agent", "task": "Agent"}

def canonical_tool(name):
    return TOOL_NAMES.get((name or "").lower(), name)

def alias_of(name):
    n = name.lower().split("/")[-1]
    for a in ("opus", "sonnet", "fable", "haiku"):
        if a in n:
            return a
    return n

def version_key(name):
    return tuple(int(x) for x in "".join(c if c.isdigit() else " " for c in name).split())

COLUMNS = ["id", "when", "model", "out_tokens", "tool_calls", "requests", "gates",
           "parallel", "pred_s", "range_s", "actual_s", "actual_out", "actual_tools", "note"]

def read_history():
    rows = []
    if os.path.exists(HISTORY):
        with open(HISTORY) as fh:
            head = fh.readline().rstrip("\n").split("\t")
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) == len(head):
                    rows.append(dict(zip(head, parts)))
    return rows

def write_history(rows):
    with open(HISTORY, "w") as fh:
        fh.write("\t".join(COLUMNS) + "\n")
        for r in rows:
            fh.write("\t".join(str(r.get(k, "")) for k in COLUMNS) + "\n")

def with_history(mutate):
    mode = "r+" if os.path.exists(HISTORY) else "w+"
    with open(HISTORY, mode) as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            text = fh.read()
            head = text.split("\n", 1)[0].rstrip("\n").split("\t") if text.strip() else []
            rows = []
            for line in text.split("\n")[1:]:
                parts = line.rstrip("\n").split("\t")
                if head and len(parts) == len(head):
                    rows.append(dict(zip(head, parts)))
            out = mutate(rows)
            cols = head or COLUMNS
            for r in rows:
                for k in r:
                    if k not in cols:
                        cols.append(k)
            fh.seek(0)
            fh.truncate()
            fh.write("\t".join(cols) + "\n")
            for r in rows:
                fh.write("\t".join(str(r.get(k, "")) for k in cols) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return out

def compute_bias():
    by = {}
    for r in read_history():
        if not r.get("actual_s"):
            continue
        try:
            pred, act = float(r["pred_s"]), float(r["actual_s"])
        except (KeyError, ValueError):
            continue
        if pred <= 0 or act <= 0:
            continue
        size = None
        try:
            if r.get("actual_out") and float(r["out_tokens"]) > 0:
                size = float(r["actual_out"]) / float(r["out_tokens"])
        except ValueError:
            size = None
        by.setdefault(r.get("model", "?"), []).append((act / pred, pred, size))

    def clamp(x):
        return round(min(max(x, 0.75), 1.6), 3)

    def entry(vs):
        return {
            "bias": clamp(statistics.median(v[0] for v in vs)),
            "n": len(vs),
            "median_pred_s": round(statistics.median(v[1] for v in vs)),
            "size_ratio": round(statistics.median(v[2] for v in vs), 2)
            if all(v[2] for v in vs) else None,
            "size_n": sum(1 for v in vs if v[2]),
        }

    everything = [v for vs in by.values() for v in vs]
    out = {"all": 1.0, "n": len(everything), "by_model": {}}
    if len(everything) >= 3:
        out["all"] = clamp(statistics.median(v[0] for v in everything))
        out["median_pred_s"] = round(statistics.median(v[1] for v in everything))
    for k, vs in by.items():
        if len(vs) >= 3:
            out["by_model"][k] = entry(vs)
    return out

def resolve_model(rates, want):
    models = rates.get("models", {})
    if want in models:
        return want, models[want]
    w = want.lower()
    hits = [k for k in models if k.lower() == w or models[k].get("alias") == w]
    if not hits:
        hits = [k for k in models if w in k.lower()]
    if not hits:
        return None, None
    hits.sort(key=lambda k: (version_key(k), models[k]["requests"]), reverse=True)
    return hits[0], models[hits[0]]

def dur(sec):
    sec = float(sec)
    if sec < 1:
        return f"{sec*1000:.0f}ms"
    if sec < 90:
        return f"{sec:.0f}s"
    m, s = divmod(int(round(sec)), 60)
    if m < 90:
        return f"{m}m {s:02d}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if m else f"{h}h"

def parse_tools(spec):
    out = {}
    for part in (spec or "").replace(",", " ").split():
        if not part:
            continue
        name, cnt = part.rsplit(":", 1) if ":" in part else (part, "1")
        try:
            c = int(cnt)
        except ValueError:
            c = 1
        out[name.lower()] = out.get(name.lower(), 0) + c
    return out

def tool_lookup(rates, name):
    tools = rates.get("tools", {})
    for k, v in tools.items():
        if k.lower() == name.lower():
            return k, v
    for k, v in tools.items():
        if name.lower() in k.lower():
            return k, v
    return None, None

def parse_actual(s):
    s = s.strip().lower().replace(" ", "")
    total, num = 0.0, ""
    for ch in s:
        if ch.isdigit() or ch == ".":
            num += ch
            continue
        if num:
            total += float(num) * {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(ch, 0)
            num = ""
    if num:
        total += float(num) * 60
    return total

def print_rates(rates, only=None):
    if not rates.get("models"):
        print("No calibration yet. Run: timeplan.py calibrate")
        return
    print(f"measured on this machine · {rates.get('files','?')} transcripts · "
          f"{rates.get('total_out_tokens',0)/1e6:.0f}M output tokens · "
          f"{rates.get('total_api_hours',0):.0f}h of API time · {rates.get('generated','')[:10]}")
    for name, src in sorted((rates.get("sources") or {}).items()):
        print(f"  source {name:8s} {src['transcripts']:5d} transcripts · {src['requests']:7,d} "
              f"requests · {src['out_tokens']/1e6:5.1f}M tokens · {src['api_hours']:6.1f}h API "
              f"· {src['turns']:,} turns")
    print()
    print(f"{'model':32s} {'req':>7s} {'decode':>7s} {'avg':>7s} {'tok/hour':>10s} "
          f"{'first':>8s} {'next':>7s} {'scale':>6s} {'fit':>9s}")
    for name, m in sorted(rates["models"].items(), key=lambda kv: -kv[1]["requests"]):
        if only and only not in name.lower() and only != m.get("alias"):
            continue
        fit = (f"{m['spread_p10']:.2f}-{m['spread_p90']:.2f}"
               if m.get("spread_p10") else "-")
        print(f"{name:32s} {m['requests']:7d} {m['decode_tok_s']:7.0f} {m['tok_s']:7.1f} "
              f"{m['tok_per_hour']:10,} {m['first_req_s']:7.2f}s {m['next_req_s']:6.2f}s "
              f"{m.get('turn_scale', 1):6.2f} {fit:>9s}")
    print("  decode = steady generation tok/s · avg = tokens ÷ all API seconds (overhead in)")
    print("  first = fixed cost of a turn's first request · next = of every request after a tool call")
    print()
    t = rates.get("turn") or {}
    if t:
        print(f"per turn (prompt -> finished answer), {t.get('turns',0):,} measured: "
              f"p50 {t.get('out_tokens_p50',0):,} tokens / {t.get('wall_s_p50',0):.0f}s · "
              f"p90 {t.get('out_tokens_p90',0):,} tokens / {t.get('wall_s_p90',0):.0f}s")
        print(f"{'':40s}  mean {t.get('tok_per_turn',0):,} tokens, {t.get('req_per_turn',0)} "
              f"requests, {t.get('tools_per_turn',0)} tool calls, "
              f"{t.get('wall_s_per_turn',0):.0f}s · {t.get('req_per_tool',0)} requests per tool call")
    c = rates.get("code") or {}
    if c:
        print(f"code: 1 line ≈ {c['line_tokens']} output tokens (measured over {c['lines']:,} lines)")
    print()
    print(f"{'tool':20s} {'calls':>7s} {'median':>9s} {'p90':>9s}")
    for name, v in sorted(rates["tools"].items(), key=lambda kv: -kv[1]["calls"]):
        print(f"{name:20s} {v['calls']:7d} {dur(v['med_s']):>9s} {dur(v['p90_s']):>9s}")
    b = rates.get("bias") or {}
    if b.get("n"):
        print()
        print(f"bias from {b['n']} finished estimates (actual/estimate): {b['all']:.2f}")

QUICK = os.path.join(HERE, "QUICK.md")

def cmd_quick(args):
    rates = json.load(open(RATES)) if os.path.exists(RATES) else {}
    if not rates.get("models"):
        sys.exit("no rates.json, run: timeplan.py calibrate")
    t = rates.get("turn") or {}
    code = rates.get("code") or {}
    lines = [
        "# timeplan quick sheet",
        "",
        f"Measured on this machine {rates.get('generated','')[:10]} from "
        f"{rates.get('total_out_tokens',0)/1e6:.0f}M output tokens over "
        f"{rates.get('total_api_hours',0):.0f}h of API time. Regenerate: `timeplan.py quick`.",
        "Full rules and traps: SKILL.md. Fresh estimate: `timeplan.py estimate ...`.",
        "",
        "| model | decode tok/s | tokens/hour | first request | each next request |",
        "|---|---|---|---|---|",
    ]
    for name, m in sorted(rates["models"].items(), key=lambda kv: -kv[1]["requests"]):
        if m["requests"] < 50:
            continue
        lines.append(f"| {name} | {m['decode_tok_s']:.0f} | {m['tok_per_hour']:,} | "
                     f"{m['first_req_s']:.1f}s | {m['next_req_s']:.1f}s |")
    lines += [
        "",
        "agent seconds = (first + (requests-1) × next + tokens ÷ decode) × scale + tool seconds",
        "requests = tool calls + 1 (measured 1.0 per tool call); scale is per model, 0.5 to 1.3.",
        "",
        f"Sizes: median turn {t.get('out_tokens_p50',0):,} tokens / {t.get('wall_s_p50',0):.0f}s · "
        f"mean turn {t.get('tok_per_turn',0):,} / {t.get('wall_s_per_turn',0):.0f}s · "
        f"p90 turn {t.get('out_tokens_p90',0):,} / {t.get('wall_s_p90',0):.0f}s · "
        f"1 line of code {code.get('line_tokens',11.5)} tokens.",
        "",
        "Tools (median / p90): " + " · ".join(
            f"{n} {v['med_s']:g}s/{v['p90_s']:g}s" for n, v in
            sorted(rates["tools"].items(), key=lambda kv: -kv[1]["calls"])[:8]),
        "",
        "Human: 1500 tokens/hour of one person. Say both clocks, always with a range.",
        "Never state a duration you did not compute. See SKILL.md.",
        "",
    ]
    with open(QUICK, "w") as fh:
        fh.write("\n".join(lines))
    print(f"wrote {QUICK}")

def cmd_show(args):
    rates = json.load(open(RATES)) if os.path.exists(RATES) else {}
    if args.json:
        print(json.dumps(rates, indent=1))
        return
    print_rates(rates, args.model)

def size_work(rates, args):
    code_tok = (rates.get("code") or {}).get("line_tokens", 10)
    turn = rates.get("turn") or {}
    if getattr(args, "lines", None):
        return round(args.lines * (code_tok if args.kind == "code" else 14))
    if getattr(args, "out", None):
        return args.out
    if getattr(args, "turns", None):
        return round(args.turns * turn.get("tok_per_turn", 4500))
    return None

def compute(rates, model_want, out, tools, gates=0, gate_sec=5.0, parallel=1.0,
            human_rate=1500.0, requests=None, extra_requests=0):
    model_name, m = resolve_model(rates, model_want)
    if not m:
        return {"error": f"unknown model {model_want!r}; known: {', '.join(rates['models'])}"}
    turn = rates.get("turn") or {}
    n_tools = sum(tools.values())
    if requests:
        n_req = requests
    elif n_tools:
        n_req = max(1, round(n_tools * turn.get("req_per_tool", 1.0)))
    else:
        n_req = 1
    n_req += extra_requests

    tool_rows, tool_s_med, tool_s_p90, unknown = [], 0.0, 0.0, []
    for name, cnt in tools.items():
        real, tv = tool_lookup(rates, name)
        if not tv:
            unknown.append(name)
            med, p90 = 1.0, 10.0
        else:
            med, p90 = tv["med_s"], tv["p90_s"]
        tool_rows.append((real or name, cnt, med, p90))
        tool_s_med += cnt * med
        tool_s_p90 += cnt * p90

    bias = rates.get("bias") or {"all": 1.0}
    entry = (bias.get("by_model", {}) or {}).get(model_name) or {
        "bias": bias.get("all", 1.0), "n": bias.get("n", 0),
        "median_pred_s": bias.get("median_pred_s")}
    b = entry.get("bias", 1.0) or 1.0
    dec = m["decode_tok_s"]
    first, nxt = m["first_req_s"], m["next_req_s"]
    par = max(parallel, 1.0)

    ov_s = first + max(n_req - 1, 0) * nxt
    gen_s = out / dec if dec else 0.0
    scale = m.get("turn_scale", 1.0)
    model_s = (ov_s + gen_s) * scale
    raw = (model_s + tool_s_med) / par + gates * gate_sec
    mid = entry.get("median_pred_s")
    applied = b if not mid else 1 + (b - 1) * min(1.0, raw / mid)
    base = (model_s * applied + tool_s_med) / par + gates * gate_sec
    low = base * min(max(m.get("spread_p10", 0.6), 0.35), 1.0)
    high = base * max(min(m.get("spread_p90", 2.0), 3.0), 1.0)
    return {
        "model": model_name,
        "out_tokens": out,
        "requests": n_req,
        "tools": {k: v for k, v in sorted(tools.items())},
        "tool_rows": tool_rows,
        "unknown_tools": unknown,
        "n_tools": n_tools,
        "overhead_s": ov_s,
        "generation_s": gen_s,
        "first_s": first,
        "next_s": nxt,
        "decode_tok_s": dec,
        "scale": scale,
        "tool_s": tool_s_med,
        "gate_s": gates * gate_sec,
        "gates": gates,
        "parallel": par,
        "bias": b,
        "bias_applied": applied,
        "bias_n": entry.get("n", 0),
        "bias_median_s": mid,
        "size_ratio": entry.get("size_ratio"),
        "base_s": base,
        "low_s": low,
        "high_s": high,
        "human_s": out / (human_rate / 3600.0),
    }

def cmd_estimate(args):
    rates = json.load(open(RATES)) if os.path.exists(RATES) else {}
    if not rates.get("models"):
        sys.exit("no rates.json, run: timeplan.py calibrate")
    out = size_work(rates, args)
    if not out:
        sys.exit("give --out <tokens>, --lines <n> [--kind code|prose], or --turns <n>")
    e = compute(rates, args.model, out, parse_tools(args.tools), args.gates, args.gate_sec,
                args.parallel, args.human_rate, args.requests, args.extra_requests)
    if "error" in e:
        sys.exit(e["error"])
    model_name, m = e["model"], rates["models"][e["model"]]
    n_req, n_tools = e["requests"], e["n_tools"]
    tools, tool_rows, unknown = e["tools"], e["tool_rows"], e["unknown_tools"]
    dec, first, nxt = e["decode_tok_s"], e["first_s"], e["next_s"]
    ov_s, gen_s, scale = e["overhead_s"], e["generation_s"], e["scale"]
    tool_s_med, par = e["tool_s"], e["parallel"]
    base, low, high, human_s = e["base_s"], e["low_s"], e["high_s"], e["human_s"]
    if args.pessimistic:
        base = high

    def add(rows):
        idx = len(rows) + 1
        rows.append({
            "id": idx,
            "when": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "model": model_name,
            "out_tokens": out,
            "tool_calls": ",".join(f"{k}:{v}" for k, v in sorted(tools.items())),
            "requests": n_req,
            "gates": args.gates,
            "parallel": f"{par:g}",
            "pred_s": round(base),
            "range_s": f"{round(low)}-{round(high)}",
            "actual_s": "",
            "note": args.note or "",
        })
        return idx

    idx = with_history(add)

    if args.json:
        print(json.dumps({
            "id": idx,
            "model": model_name,
            "out_tokens": out,
            "requests": n_req,
            "tool_calls": {k: v for k, v in sorted(tools.items())},
            "parallel": par,
            "gates": args.gates,
            "model_seconds": round(ov_s + gen_s, 1),
            "tool_seconds": round(tool_s_med, 1),
            "gate_seconds": round(args.gates * args.gate_sec, 1),
            "agent_seconds": round(base),
            "low_seconds": round(low),
            "high_seconds": round(high),
            "human_seconds": round(human_s),
            "human_rate_per_hour": args.human_rate,
            "scale": scale,
            "spread": [m.get("spread_p10"), m.get("spread_p90")],
            "calibrated": rates.get("generated", "")[:10],
        }, indent=1))
        return

    print(f"estimate #{idx} · {model_name} · {out:,} output tokens · {n_tools} tool calls "
          f"= {n_req} requests · {par:g} agent{'s' if par > 1 else ''}")
    if abs(e["bias_applied"] - 1) > 0.02:
        print(f"  calib     {e['bias']:.2f}x from {e['bias_n']} finished jobs of about "
              f"{dur(e['bias_median_s'])}; applied {e['bias_applied']:.2f}x at this size")
    print(f"  model     {dur(first)} first request + {max(n_req-1,0)} × {dur(nxt):>5s} "
          f"= {dur(ov_s):>8s}  +  {out:,} tok ÷ {dec:.0f} tok/s = {dur(gen_s):>8s}"
          f"   ({dur(ov_s+gen_s)})")
    for name, cnt, med, p90 in sorted(tool_rows, key=lambda r: -r[1] * r[2]):
        print(f"  tool      {cnt:>3d} × {name:<12s} {dur(med):>7s} = {dur(cnt*med):>8s}"
              f"   (p90 {dur(cnt*p90)})")
    if unknown:
        print(f"  tool      no measurement for {', '.join(unknown)}, assumed 1s and p90 10s")
    if args.gates:
        print(f"  waiting   {args.gates} × {args.gate_sec:g}s human gate "
              f"= {dur(args.gates*args.gate_sec):>8s}")
    print("  " + "-" * 70)
    print(f"  agent     ~{dur(base)}   range {dur(low)} to {dur(high)}")
    print(f"  human     {args.human_rate:,.0f} tok/h means {dur(human_s)} of one person"
          f"   ({human_s/base:,.0f}× slower than the agent)")
    print()
    print(f"Say: \"about {dur(base)} of agent time ({dur(low)} to {dur(high)}), against "
          f"{dur(human_s)} of human work\".")
    print(f"When it is done: timeplan.py done {idx} 12m")

def cmd_schedule(args):
    rates = json.load(open(RATES)) if os.path.exists(RATES) else {}
    if not rates.get("models"):
        sys.exit("no rates.json, run: timeplan.py calibrate")
    spec = json.load(open(args.file))
    steps = spec.get("steps") if isinstance(spec, dict) else spec
    if not steps:
        sys.exit("the plan file needs a list of steps")
    gate_sec = (spec.get("gate_sec") if isinstance(spec, dict) else None) or args.gate_sec
    human_rate = (spec.get("human_rate") if isinstance(spec, dict) else None) or args.human_rate

    clock = None
    if args.start:
        hh, mm = args.start.split(":")
        now = dt.datetime.now(dt.timezone.utc).astimezone()
        clock = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)

    rows, total, total_low, total_high, human, cur = [], 0.0, 0.0, 0.0, 0.0, 0.0
    print(f"{'#':>2s} {'step':30s} {'model':26s} {'tokens':>8s} {'tools':>5s} {'time':>9s} "
          f"{'at':>7s}")
    for i, st in enumerate(steps, 1):
        out = st.get("out") or st.get("tokens")
        if not out and st.get("lines"):
            out = round(st["lines"] * (rates.get("code") or {}).get("line_tokens", 11.5))
        if not out:
            sys.exit(f"step {i} has no out/tokens/lines")
        e = compute(rates, st.get("model", args.model), out,
                    parse_tools(st.get("tools") or ""), st.get("gates", 0), gate_sec,
                    st.get("parallel", 1), human_rate, st.get("requests"))
        if "error" in e:
            sys.exit(f"step {i}: {e['error']}")
        base = e["high_s"] if args.pessimistic else e["base_s"]
        cur += base
        total += base
        total_low += e["low_s"]
        total_high += e["high_s"]
        human += e["human_s"]
        when = ""
        if clock:
            when = (clock + dt.timedelta(seconds=cur)).strftime("%H:%M")
        rows.append((i, st.get("name", "")[:30], e["model"], out, e["n_tools"],
                     dur(base), (when or dur(cur))))
    for r in rows:
        print(f"{r[0]:2d} {r[1]:30s} {r[2]:26s} {r[3]:8,} {r[4]:5d} {r[5]:>9s} {r[6]:>7s}")
    print("  " + "-" * 94)
    print(f"   {'total agent time':30s} {'':26s} {'':8s} {'':5s} {dur(total):>9s}"
          f"   range {dur(total_low)} to {dur(total_high)}")
    print(f"   {'human time for the same work':30s} {'':26s} {'':8s} {'':5s} {dur(human):>9s}")
    if args.start:
        print(f"   start {args.start}, done about "
              f"{(clock + dt.timedelta(seconds=total)).strftime('%H:%M')}"
              f" (range {(clock + dt.timedelta(seconds=total_low)).strftime('%H:%M')} to "
              f"{(clock + dt.timedelta(seconds=total_high)).strftime('%H:%M')})")
    if not args.json:
        print()
        print("Steps run one after another unless a step sets parallel>1. Human gates are in "
              "the per-step time, not extra.")

    def add(rows_h):
        idx = len(rows_h) + 1
        rows_h.append({
            "id": idx, "when": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "model": "schedule(" + str(len(steps)) + ")", "out_tokens": sum(r[3] for r in rows),
            "tool_calls": "", "requests": "", "gates": "", "parallel": "",
            "pred_s": round(total), "range_s": f"{round(total_low)}-{round(total_high)}",
            "actual_s": "", "note": args.note or "",
        })
        return idx

    idx = with_history(add)
    if args.json:
        print(json.dumps({
            "id": idx,
            "steps": [{"name": st.get("name", ""), "model": e} for st, e in
                      zip(steps, [r[2] for r in rows])],
            "total_seconds": round(total),
            "low_seconds": round(total_low),
            "high_seconds": round(total_high),
            "human_seconds": round(human),
            "finished_at": (clock + dt.timedelta(seconds=total)).strftime("%H:%M") if clock else None,
        }, indent=1))
    else:
        print(f"When it is done: timeplan.py done {idx} 45m")

def read_lessons(limit=None):
    if not os.path.exists(LESSONS):
        return []
    lines = [l.rstrip("\n") for l in open(LESSONS) if l.strip().startswith("-")]
    return lines[-limit:] if limit else lines


def add_lesson(text):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    new = not os.path.exists(LESSONS)
    with open(LESSONS, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        if new:
            fh.write("# timeplan lessons\n\n"
                     "One dated line per lesson, written by `done` when a job missed, or by `learn` when a\n"
                     "human or an agent notices something the numbers do not say. Read this before sizing.\n\n")
        fh.write(f"- {stamp} {text}\n")
        fcntl.flock(fh, fcntl.LOCK_UN)
    return text


def cmd_learn(args):
    text = " ".join(args.text).strip()
    if not text:
        sys.exit("give one sentence, for example: learn \"refactors here need two test runs, not one\"")
    add_lesson(text)
    print(f"lesson recorded ({len(read_lessons())} total): {text}")


def cmd_review(args):
    rows = read_history()
    rates = json.load(open(RATES)) if os.path.exists(RATES) else {}
    done = [r for r in rows if r.get("actual_s")]
    print(f"estimates recorded: {len(rows)}, with a real duration: {len(done)}")
    if done:
        print()
        print(f"{'#':>3s} {'model':24s} {'planned':>9s} {'predicted':>10s} {'actual':>9s} {'ratio':>6s}")
        for r in done:
            try:
                pred, act = float(r["pred_s"]), float(r["actual_s"])
            except (KeyError, ValueError):
                continue
            if args.model and args.model not in r.get("model", ""):
                continue
            print(f"{r['id']:>3s} {r.get('model','')[:24]:24s} {int(float(r.get('out_tokens') or 0)):>9d} "
                  f"{dur(pred):>10s} {dur(act):>9s} {act/pred:6.2f}")
        missing = [r["id"] for r in rows if not r.get("actual_s")]
        if missing:
            print()
            print(f"no actual yet: #{', #'.join(missing)}  (timeplan.py done <id> 12m)")
    lessons = read_lessons()
    if lessons:
        print()
        print(f"lessons on file: {len(lessons)}, last {min(len(lessons), 5)}:")
        for line in lessons[-5:]:
            print(f"  {line[2:][:110]}")
    b = (rates.get("bias") or {})
    if b.get("n"):
        print()
        print(f"bias in use: {b.get('all', 1):.2f}x from {b['n']} finished estimate(s), "
              f"clamped to 0.75 to 1.6, applied at the size it was learned from")
    else:
        print()
        print("bias: none yet, three finished estimates are needed")


def cmd_done(args):
    actual = parse_actual(args.actual)
    if actual <= 0:
        sys.exit(f"cannot read duration {args.actual!r} (use 12m, 90s, 1h20m)")

    def mark(rows):
        if not rows:
            return None
        idx = args.id or len(rows)
        if not 1 <= idx <= len(rows):
            return None
        row = rows[idx - 1]
        row["actual_s"] = round(actual)
        if args.out:
            row["actual_out"] = args.out
        if args.tools:
            row["actual_tools"] = args.tools
        if args.note:
            row["note"] = args.note
        return row

    row = with_history(mark)
    if row is None:
        sys.exit(f"no estimate #{args.id}" if args.id else "no estimates recorded yet")
    pred = float(row["pred_s"])
    if os.path.exists(RATES):
        rates = json.load(open(RATES))
        rates["bias"] = compute_bias()
        with open(RATES, "w") as fh:
            json.dump(rates, fh, indent=1)
    if not 0.8 <= actual / pred <= 1.25:
        detail = f"#{row['id']} {row.get('model','?')}: estimated {dur(pred)} with " \
                 f"{row.get('out_tokens','?')} planned tokens, took {dur(actual)} " \
                 f"({actual/pred:.2f}x)"
        if args.out and row.get("out_tokens"):
            try:
                detail += f", the job used {float(args.out)/float(row['out_tokens']):.2f}x the tokens planned"
            except (ValueError, ZeroDivisionError):
                pass
        if args.tools:
            detail += f", tools: {args.tools}"
        add_lesson(detail)

    line = f"#{row['id']}: estimated {dur(pred)}, actual {dur(actual)} ({actual/pred:.2f}× the estimate)"
    if args.out and row.get("actual_out") and row.get("out_tokens"):
        try:
            grew = float(args.out) / float(row["out_tokens"])
            line += f", the job used {grew:.2f}× the tokens you planned"
        except (ValueError, ZeroDivisionError):
            pass
    print(line)

def main():
    ap = argparse.ArgumentParser(prog="timeplan", description=DESC,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    c = sub.add_parser("calibrate", help="rescan transcripts into rates.json")
    c.add_argument("--full", action="store_true", help="ignore the per-file cache")
    c.add_argument("--source", choices=["claude", "pi", "all"], default="all")
    c.add_argument("--quiet", action="store_true")
    c.set_defaults(fn=calibrate)

    q = sub.add_parser("quick", help="regenerate QUICK.md (the 25-line sheet)")
    q.set_defaults(fn=cmd_quick)

    s = sub.add_parser("show", help="print the measured rates")
    s.add_argument("--model")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_show)

    e = sub.add_parser("estimate", help="time a planned piece of work")
    e.add_argument("--model", "-m", default="opus",
                   help="opus | sonnet | fable | haiku or a full model name")
    e.add_argument("--out", type=int, help="expected output tokens across all requests")
    e.add_argument("--lines", type=int, help="size the work by lines instead of tokens")
    e.add_argument("--kind", default="code", choices=["code", "prose"])
    e.add_argument("--turns", type=float, help="size by turns (median turn is in `show`)")
    e.add_argument("--tools", "-t", default="", help='e.g. "bash:6 read:20 edit:8"')
    e.add_argument("--requests", type=int, help="override the request count")
    e.add_argument("--extra-requests", type=int, default=0,
                   help="add text-only requests (plan, summarize)")
    e.add_argument("--gates", type=int, default=0, help="human approval/question stops")
    e.add_argument("--gate-sec", type=float, default=5.0)
    e.add_argument("--parallel", type=float, default=1, help="agents sharing the work")
    e.add_argument("--human-rate", type=float, default=1500, help="tokens/hour of one person")
    e.add_argument("--pessimistic", action="store_true")
    e.add_argument("--note", default="")
    e.add_argument("--json", action="store_true", help="machine-readable, for other agents")
    e.set_defaults(fn=cmd_estimate)

    sc = sub.add_parser("schedule", help="time a multi-step plan from a JSON file")
    sc.add_argument("file", help='a JSON list of steps, for example [{"name":"api","model":"opus","out":20000,"tools":"bash:5"}]')
    sc.add_argument("--model", "-m", default="opus", help="default model for steps without one")
    sc.add_argument("--start", help="clock time to count from, e.g. 15:00")
    sc.add_argument("--gate-sec", type=float, default=5.0)
    sc.add_argument("--human-rate", type=float, default=1500)
    sc.add_argument("--pessimistic", action="store_true")
    sc.add_argument("--json", action="store_true")
    sc.add_argument("--note", default="")
    sc.set_defaults(fn=cmd_schedule)

    lr = sub.add_parser("learn", help="record one lesson that the next planner should read")
    lr.add_argument("text", nargs="+")
    lr.set_defaults(fn=cmd_learn)

    r = sub.add_parser("review", help="past estimates against what really happened")
    r.add_argument("--model")
    r.set_defaults(fn=cmd_review)

    d = sub.add_parser("done", help="record the real duration of an estimate")
    d.add_argument("id", nargs="?", type=int)
    d.add_argument("actual", help="e.g. 12m, 90s, 1h20m")
    d.add_argument("--out", type=int, help="output tokens the job really used, if you know")
    d.add_argument("--tools", help="tool calls the job really made, e.g. \"bash:40 read:70\"")
    d.add_argument("--note", default="")
    d.set_defaults(fn=cmd_done)

    args = ap.parse_args()
    if not getattr(args, "fn", None):
        ap.print_help()
        return
    args.fn(args)

if __name__ == "__main__":
    main()
