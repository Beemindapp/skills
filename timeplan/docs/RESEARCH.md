# timeplan: research and provenance

Every number in `SKILL.md` and `QUICK.md` comes from this machine's own transcripts. Every rule in the skill comes
either from that data or from a source below. When a rule and the data disagree, the data wins. A rule without a
source should be deleted.

## 1. Why measured numbers instead of estimates

The failure this skill fixes has a name and a literature.

- Kahneman and Tversky named the **planning fallacy** in 1979: people underestimate how long work takes, even with
  a history of overruns, because they plan from the inside view (the intended steps) instead of the outside view
  (what similar work actually took). https://thedecisionlab.com/biases/planning-fallacy
- The primary study is Buehler, Griffin and Ross (1994), *Exploring the planning fallacy: why people underestimate
  their task completion times*. Psychology students predicted their own thesis completion; **only 30 % finished
  within their own estimate**, and the overrun survived asking for a worst case. In classrooms, students report
  finishing about two-thirds of assignments later than predicted. Both figures come from the Decision Lab summary,
  which is the source I read; the paper itself was not fetched, so treat 30 % as second-hand.
- The documented fix is **reference class forecasting**: predict from the measured distribution of comparable past
  work instead of from the current plan. It reduces time and cost overruns. Same source.

This skill is that fix applied to agent work. The reference class is every finished turn in `~/.claude/projects`
and the pi session directory. The agent only sizes the work (tokens, tool calls). The distribution and
the arithmetic come from the script.

## 2. Human time against agent time (METR time horizons)

A METR time horizon is the human-expert task length at which an agent succeeds with a given reliability. Measured
50 % horizons from METR's public data, in minutes of human work:

| agent | 50 % horizon |
|---|---|
| claude-mythos-preview (early) | 1045 min (17.4 h, above METR's 16 h reliability limit) |
| claude-opus-4.6 | 719 min (12.0 h) |
| gemini-3.1-pro | 384 min (6.4 h) |
| gpt-5.2 | 352 min (5.9 h) |
| gpt-5.3-codex | 350 min (5.8 h) |
| gpt-5.4 | 342 min (5.7 h) |
| claude-opus-4.5 | 293 min (4.9 h) |
| gemini-3-pro | 224 min (3.7 h) |
| gpt-5.1-codex-max | 224 min (3.7 h) |

Latest point from the November 2026 update: "up to 27 hours" on the verifiable subset of their newer TH2 suite, and
a 50 % horizon of 2 hours 17 minutes for GPT-5 back in August 2025, which frames how fast the numbers move.

Doubling time of the 50 % horizon, all measured in human expert task duration:

| window | doubling time |
|---|---|
| combined TH1.1 and TH2, Nov 2026 | 3.61 months, 110 days (95 % CI 2.08 to 4.94) |
| TH1.1 alone, for comparison | 105 days |
| TH2 verifiable subset alone | 1.65 months (95 % CI 0.79 to 3.99) |
| original TH1 suite | 196 days, about 7 months |
| from 2023 on, TH1.1 | 131 days, 128.7 in the raw file (CI 104.4 to 158.0) |
| since 2024, TH1.1 | 89 days |

METR's own projection from that trend puts the 200-hour threshold of their suite around July 2027. Treat that as a
projection, not a measurement.

Re-check this section yearly. The numbers age faster than the method does.

- https://metr.org/assets/benchmark_results_1_1.yaml
- https://metr.org/blog/2026-11-20-q4-2026-capability-update/
- https://metr.org/time-horizons/

Three statements from METR shaped the skill more than the numbers did:

1. A time horizon is not wall clock. It is a difficulty measure expressed in human-expert time. METR does not
   report how long the agent itself takes, "because it varies greatly by inference provider and exact agent
   setup". The agent's own wall clock has to be measured where it runs. That is this skill.
2. Agents are "typically several times faster than humans on tasks they complete successfully ... because they
   take fewer actions". Agent minutes and human hours are different clocks. Never convert one into the other
   silently.
3. A "two-hour task" means what a low-context person could do in two hours, such as a new hire or a freelancer,
   not what the owner could do in their own repo. The human clock in `estimate` therefore says "of one person".

Two cautions on reading a horizon:

- It is an aggregate, not per-task reliability. On tasks that take a human 90 minutes to 3 hours, METR reports GPT-5
  succeeding always on about a third of them and failing always on another third, so the 50 % figure describes the
  mixture. Tasks above 16 hours are unreliable with the current suite, which is why the 17.4 h row above is marked.
- From the November 2026 update: models match or beat skilled humans on verifiable coding and optimisation tasks
  with fast feedback, but show worse judgement where feedback is slow, and agent swarms help only where a fast
  metric exists. The skill treats review, judgement and slow feedback as human gates (`--gates`), never as agent
  work.

## 3. Throughput: why the numbers are local

Published throughput tables measure one provider, one scaffold, one moment, with no tools and no 250k-token
context. METR's statement above reaches the same conclusion from another direction. So `rates.json` is built only
from first-party transcripts:

- Claude Code: `~/.claude/projects/**/*.jsonl`. `usage.output_tokens` plus request wall clock from the
  `api-request` record. One API response is logged as several assistant entries, one per content block, so they
  are folded by message id before anything is measured.
- pi: `~/.pi/agent/sessions/**/*.jsonl` by default (`TIMEPLAN_PI_ROOT` to point elsewhere). Each assistant message carries `durationMs` and its own
  `usage.output`. Tool results carry `durationMs` too. A pi message is stamped with the moment its request
  started, so the reply lands `durationMs` later.
- Measured on 2026-11-21: 1,150 transcripts, 63k Claude requests plus 16k pi requests, 69M output tokens, 222h of
  API time. `timeplan.py calibrate` reproduces it in about 35 s first time and 0.3 s after, because only changed
  files are re-read.

External claims worth comparing against, and where they disagree:

| claim | source | against the local data |
|---|---|---|
| Sonnet 5.5 streams at about 200 tok/s | a vendor latency comparison, seen only as a search snippet, not verified on the page | measured here at 161 tok/s decode and 116 tok/s average. The gap is what a clean benchmark and a real 200k-context agent session do to each other, and it is the reason the skill does not borrow published rates. |
| performance "consistently degrades with increasing input length" | Chroma, context rot (https://www.trychroma.com/research/context-rot). Numeric deltas are in charts only, so no figure is quoted here | not contradicted. Decode *speed* is flat with context on this machine; accuracy is a different axis. |
| monitors miss a dangerous action 2x to 30x more often after 800k tokens of benign activity | arXiv 2605.12366, on Opus 4.6, GPT 5.4 and Gemini 3.1 as classifiers | long context buys correctness problems, not seconds. The skill treats "read the whole repo again" as a token cost, never as a slowdown. |
| per-model output tok/s and time per task | Artificial Analysis publishes both, but the values render in JS charts and were not extractable | unused. Same reason as the row above: provider-specific, scaffold-free, and not this machine. |

Checks that were run:

- The estimator, `requests x overhead + tokens / decode`, scaled per model, against 900 random measured turns from
  both agents: median predicted over actual **0.93**, 80 % within 2x, 90 % within 3x, p10 and p90 at 0.41 and
  1.60. Per-model medians sit between 0.78 and 1.10. `tests/validate.py` reruns it and prints the breakdown.
- Context length: decode speed is flat from 10k to 250k tokens of context. Median latency of small requests rose
  only from 2.37 s to 3.85 s between 36k and 559k of context. There is no context term in the model, by
  measurement.

## 4. What each finding changed in the skill

1. Reference class instead of inside view: `estimate` and `schedule` exist at all. The agent supplies tokens and
   tool calls, never the arithmetic.
2. Planning-fallacy overruns survive worst-case thinking: the range comes from the measured p10 and p90 of past
   turns, not from the agent imagining what could go wrong.
3. Horizon is not wall clock: the two clocks are stated separately, always.
4. METR's human baseline is low-context: human time is labelled "of one person", default 1,500 tokens per hour,
   about one line of finished code per minute. `--human-rate` exists to argue with it.
5. Agents take fewer actions than humans: tool calls, not effort, size the work, at one request per tool call
   (measured 0.97).
6. Judgement fails where feedback is slow: `--gates` for reviews and decisions, and the traps section says a human
   gate is minutes, not seconds.
7. Swarms help only with fast verifiable feedback: `--parallel` divides elapsed time only, and the skill says it
   does not divide the owner's attention.
8. Throughput is provider and scaffold specific: nothing in the skill quotes an external rate. `calibrate` says
   "unmeasured" rather than borrowing a number.

## 5. Known gaps

- **Effort levels.** `--effort high` in Claude Code and `thinkingLevel` in pi change thinking tokens, which change
  wall clock. Present in the transcripts, not yet split out per model.
- **Queue and provider load** are not modelled. They show up as the p10 tail of the per-model spread.
- **Human rates** are an assumption. The corpus holds no human timing. A real number needs the owner's own
  time-tracking cross-referenced against commits.
- **Cost** is recorded in the pi transcripts as `usage.cost`. It could be joined to time, but the skill answers
  only the time question.
