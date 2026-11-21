# timeplan quick sheet

Measured on this machine 2026-11-21 from 69M output tokens over 222h of API time. Regenerate: `timeplan.py quick`.
Full rules and traps: SKILL.md. Fresh estimate: `timeplan.py estimate ...`.

| model | decode tok/s | tokens/hour | first request | each next request |
|---|---|---|---|---|
| claude-opus-5-5 | 130 | 289,263 | 2.5s | 3.6s |
| deepseek-v4.1-flash-1017 | 183 | 412,256 | 0.6s | 1.3s |
| claude-sonnet-5-5 | 161 | 416,823 | 1.0s | 1.4s |
| claude-fable-5-1 | 91 | 226,451 | 1.5s | 3.5s |
| claude-fable-5-2 | 114 | 390,492 | 0.7s | 0.6s |
| glm-5.3-flash-uncensored | 100 | 122,371 | 2.9s | 3.8s |
| claude-haiku-5-5 | 279 | 844,290 | 1.2s | 1.1s |
| glm-5.3-uncensored | 73 | 165,543 | 3.5s | 2.7s |
| qwen3.8-27b-uncensored-fast | 207 | 539,439 | 1.8s | 2.3s |
| gemini-4-carbon | 65 | 150,251 | 4.5s | 5.0s |
| gpt-6.1-sol-pro | 45 | 147,973 | 7.5s | 4.4s |

agent seconds = (first + (requests-1) × next + tokens ÷ decode) × scale + tool seconds
requests = tool calls + 1 (measured 1.0 per tool call); scale is per model, 0.5 to 1.3.

Sizes: median turn 2,239 tokens / 36s · mean turn 9,651 / 229s · p90 turn 22,387 / 562s · 1 line of code 11.5 tokens.

Tools (median / p90): Bash 0.53s/16.79s · Read 0.08s/0.53s · Write 0.21s/1.5s · Edit 0.27s/4.73s · WebSearch 10.59s/14.96s · WebFetch 5.96s/11.89s · SubagentHandback 0.19s/0.94s · Agent 1.19s/33.5s

Human: 1500 tokens/hour of one person. Say both clocks, always with a range.
Never state a duration you did not compute. See SKILL.md.
