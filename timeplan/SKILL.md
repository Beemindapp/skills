---
name: timeplan
description: Turn agent work into honest wall-clock time from numbers measured on this machine. Use whenever a duration is about to be stated or a schedule made: "how long will this take", "when will it be done", "can we finish today", "estimate this", "plan the week", "is this a two-hour job", how much fits before a deadline, "what should I delegate", or any plan that mentions minutes, hours or days. Also use when the owner asks how fast a model is (tokens per second or per hour) or how long a subagent takes.
allowed-tools: Bash(python3 *timeplan.py *), Bash(*timeplan.py *)
---

# timeplan

You cannot feel time. Your sense of "this takes two hours" is a memory of human work, and it is wrong in both
directions at once. The agent part of a job is usually minutes. Tool time, human gates and queue wait are usually
hours. None of them scale like a person. So: **no duration leaves your mouth that a command did not
compute.** Every number here comes from machine transcripts: 1,150 sessions from Claude Code and pi, 69M output
tokens, 222h of API time. Run `calibrate` and `show` for your own.

```bash
T=~/projects/skills/timeplan/timeplan.py      # wherever you cloned it
python3 $T show                                  # measured table, per model and per tool
python3 $T estimate -m opus --out 20000 --tools "bash:6 read:20 edit:5" [--gates 2] [--parallel 1]
python3 $T estimate -m sonnet --lines 400 --kind code --tools "bash:20 read:40 edit:30" --json
python3 $T schedule plan.json --start 15:00      # multi-step plan: the script adds the steps, not you
python3 $T done 7 12m                            # after the job: record reality, the bias adapts
python3 $T calibrate                             # new model or a month (35s first, 0.3s after)
python3 $T quick                                 # regenerate QUICK.md after calibrate
```

## Five rules

1. **No number without a run.** Sizing is an opinion, timing is a measurement. Run `estimate` or `schedule`.
2. **Two clocks, always labelled.** "About 5 minutes of agent time; the same work is about 13 hours of a person."
   Never one number standing in for both, and never a human duration applied to agent work.
3. **A range plus the inputs.** Give the measured range and the two assumptions (output tokens, tool calls). The
   owner argues with the inputs, not with fake precision. If an input is a guess, say it is one.
4. **Size the work in tokens and tool calls, not in feelings.** Use the anchors below, then run the script.
5. **Agent time is not delivery time.** Add human gates with `--gates`, and never promise a clock time that
   depends on the owner sitting at the screen.

## Who is running this

| You are | Do this |
|---|---|
| Claude Code, main thread | Run `estimate` or `schedule`. It costs 50ms. Never add durations by hand. |
| A subagent, or short on context | Read `QUICK.md` (25 lines: table, formula, anchors) instead of this file, and run `estimate` if bash is allowed. Report the estimate id to the parent so it can run `done`. |
| pi, or another agent reading this as a skill | Same CLI. pi sessions are already a calibration source (`calibrate --source pi`), so subagent models are measured too. |
| An agent with no bash | Use the `QUICK.md` table and say "from the table dated <date>, not re-measured". Never invent a rate for a model that is not there. |

## Sizing the work

| The work | Output tokens | Tool calls |
|---|---|---|
| a one-question answer, no tools | 300 to 800 | 0 |
| median turn (prompt to finished answer), 36s | 2,200 | 2 |
| mean turn, one real task, about 4 min | 9,600 | 11 |
| a new module or feature, tests included | 25,000 to 60,000 | 40 to 120 |
| a refactor across 10 files, verified | 80,000 to 150,000 | 150 to 300 |
| p90 turn, a long autonomous run, 9 min | 22,000 | 11 |
| one line of code written | 11.5 tokens | none |

Lines are the easy way to size code: `--lines 200`, at 11.5 tokens per line measured over 560k lines. Prose is
about 14 tokens per line. Count tool calls one by one, one per file read, one per edit, one per shell command, and
do not round down: **each tool call costs about one more model request** (measured 0.97), and every request has
its own fixed cost.

## What the numbers are (2026-11-21, `show` refreshes them)

| model | decode tok/s | tokens/hour | first request | each next request |
|---|---|---|---|---|
| opus 5.5 | 130 | 289,000 | 2.5s | 3.6s |
| sonnet 5.5 | 161 | 417,000 | 1.0s | 1.4s |
| haiku 5.5 | 279 | 844,000 | 1.2s | 1.1s |
| fable 5.2 / 5.1 | 114 / 91 | 390,000 / 226,000 | 0.7s / 1.5s | 0.6s / 3.5s |
| deepseek v4.1 flash (subagents) | 183 | 412,000 | 0.6s | 1.3s |
| qwen 3.8 27b (subagents) | 207 | 539,000 | 1.8s | 2.3s |
| glm 5.3 flash unc. / glm 5.3 unc. | 100 / 73 | 122,000 / 166,000 | 2.9s / 3.5s | 3.8s / 2.7s |
| gemini 4 carbon | 65 | 150,000 | 4.5s | 5.0s |
| gpt 6.1 sol pro | 45 | 148,000 | 7.5s | 4.4s |

- **decode** is steady generation, thinking included. **tokens/hour** is the whole-task average with per-request
  overhead folded in. **first / next request** is the fixed cost of a request: the first of a turn pays for the
  whole context, every follow-up after a tool result pays only for its increment.
- The estimator is `requests x overhead + tokens / decode`, scaled per model so the median measured turn comes
  out right. Checked against 900 random measured turns: 80 % within 2x, 90 % within 3x, median ratio 0.93.
  `show` prints each model's p10 and p90 spread, and `estimate` uses that spread as its range.
- Tool latency, median and p90: Bash 0.5s / 17s, Read 0.08s / 0.5s, Write 0.2s / 1.5s, Edit 0.3s / 5s,
  WebSearch 11s / 15s, WebFetch 6s / 12s, Agent 1.2s / 34s, AskUserQuestion 53s / 12m, ExitPlanMode 27s / 9m.
- Sources and the external cross-check: `docs/RESEARCH.md`. Local measurement wins every disagreement.

## Delegating

`show` has a row per subagent model, so delegation is a time decision, not a taste decision. The same 20k-token
job with 10 file reads, measured: haiku 1m51, qwen 3.8 27b 2m00, sonnet 3m00, deepseek v4.1 flash 3m14, opus
3m30, glm 5.3 flash unc. 4m21, gemini 4 carbon 5m56, gpt 6.1 sol pro 7m58. Run `estimate -m <model>` for the real
number. `--parallel` divides elapsed time but not the queue, and it does not divide judgement: fast models with
slow feedback are where delegation goes wrong.

## Multi-step plans

Never add steps yourself. Arithmetic across six numbers with three ranges is where every plan goes wrong. Write
the steps to a JSON file and let the script do it:

```json
{"steps": [
  {"name": "design note", "model": "opus",     "out": 12000, "tools": "read:25 bash:4"},
  {"name": "implement",   "model": "opus",     "out": 30000, "tools": "edit:12 bash:8 read:30"},
  {"name": "tests",       "model": "sonnet",   "out": 18000, "tools": "bash:20 edit:6", "gates": 1},
  {"name": "3 subagents", "model": "deepseek", "out": 45000, "tools": "read:30", "parallel": 3}
]}
```

`schedule plan.json --start 15:00` prints per-step time, cumulative clock times, the total with its range, and the
human equivalent of the whole plan.

## What makes plans wrong

- **Overhead beats tokens for small work.** 20 file reads are 20 requests, a minute of pure context before any
  thinking. Fewer, bigger steps are genuinely faster.
- **The tail is the tool, not the model.** Bash p90 is 17s (builds, tests, installs), so five of those are two
  minutes. Every web search costs about 10s.
- **A human gate is minutes, not seconds.** Each approval, question or review costs 1 to 30 min of wall clock and
  usually sits on the critical path. Count them with `--gates`.
- **Parallel agents divide agent time, not your attention**, and they still queue on the same servers.
- **Context size barely changes decode speed.** Measured flat from 10k to 250k tokens, so it changes how many
  tokens you burn, not how fast they arrive. What it does cost is accuracy: monitors miss 2x to 30x more often at
  extreme lengths. Long context is a correctness bill, not a time bill.
- **Tokens are not progress.** A 50k-token turn that reads the repo and changes nothing is five minutes and zero
  output. Size deliverables, not effort.
- **Server load is unmodelled.** The p10 spread per model is what the range is for. You cannot predict it.
- **Human parity is nonsense.** 1,500 tokens per hour is one person's real net output, and that is about one line
  of finished code per minute: a low-context contractor, not the owner in their own repo. A 20k-token job is about
  13 hours of a person's day. Agent time is not a shortcut to it, because the human part is the decision, the
  review and the taste.

## External anchors

- A METR time horizon is the human-expert task length an agent completes 50 % of the time: about 5 h for opus 4.5,
  about 12 h for opus 4.6, and the frontier reached about 27 h on their harder TH2 verifiable subset in November
  2026, doubling roughly every 3.6 months. It is a difficulty measure, not wall clock. METR says it does not
  report agent time because it varies by provider and scaffold. That gap is what this skill fills, on this
  machine.
- METR: agents are "typically several times faster than humans on tasks they complete successfully" because they
  take fewer actions. Their human baseline is a low-context new hire, not an owner with months of context.
- Kahneman and Tversky's planning fallacy is the reason to use a measured reference class instead of reasoning
  about the plan. In the classic thesis study only 30 % finished within their own estimate, worst case included.
  Numbers and links: `docs/RESEARCH.md`.

## Answering "when will it be done"

In a few lines, in this order: agent time with its range, the human gates it needs, what is on the critical path,
and a clock time only if the owner's own availability is known: "~5 min of agent time once you answer two
questions at 15:00, so done around 15:10". If a plan needs more than two hours of agent time, say what to cut
first. A job that size should be broken into ordered steps, each with its own estimate.

## Keeping it honest

- `done <id> <actual>` after every estimate that mattered. The script stores actual against predicted in
  `history.tsv` and folds the median into `rates.json` bias, so estimates self-correct (clamped to 0.5x to 2.5x).
- `calibrate` is incremental and needs no network. Run it when a new model appears or after a month, then `quick`.
- Never quote a rate for a model that is not in `show`. Calibrate first, or say it is unmeasured.
- If `rates.json` is missing, run `calibrate` before answering anything about time.
