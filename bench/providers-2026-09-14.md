# Provider survey for the free-swarm workhorse (2026-09-14)

Goal: a 0 EUR workhorse for 20-agent swarms (10-40 tool calls, 0.3-1M tokens per
agent per turn), replacing Gemini 3.8 Flash on the Gemini API (paid, see spend line).

Smoke prompt for every row: "Create a file named hello.txt containing exactly: ok.
Then reply with the single word DONE." Timeout 180 s, auto-approve, JSON output.
Logs: `<SCRATCH>/providers/<row-dir>/{argv.txt,out.txt,err.txt,result.txt}` where
`<SCRATCH>` = `%LOCALAPPDATA%\Temp\claude\<project>\<session>\scratchpad`.

## Table

| CLI | model id (exact) | free? | limits (RPM / RPD / tokens) | context (in/out) | tools OK (smoke) | headless argv | usage field | verdict |
|---|---|---|---|---|---|---|---|---|
| opencode 1.18.30 | `opencode/big-pickle` | 0 cost (Zen stealth model, "free for a limited time", no login, 0 credentials on this machine) | undocumented; per-IP daily free quota, resets 00:00 UTC; reported errors: `429 FreeUsageLimitError "Rate limit exceeded. Please try again later."`, `Free usage exceeded, add credits https://opencode.ai/zen`, transient "too many requests" retries (GitHub issues 15714, 42765, 42977, 10404) | 200k / 32k | yes: 1 `write` call, hello.txt = `ok.` (prompt ambiguity), model span 2.9 s, wall ~14 s; probe 5/5 | `opencode run --format json -m opencode/big-pickle "<prompt>"` (run in target dir; `--dir <d>` also exists) | `step_finish` event: `part.tokens{total,input,output,reasoning,cache{read,write}}`, `part.cost` (0) | WORKHORSE |
| opencode | `opencode/muse-spark-1.3-contributor-free` | 0 cost (Zen, limited time) | same as above | 1M / 131k | yes: `write`, hello.txt = `ok` exact, 18.0 s; probe 5/5 | same with `-m opencode/muse-spark-1.3-contributor-free` | same | RUNNER-UP |
| opencode | `opencode/ling-3.0-flash-fin-free` | 0 cost | same | 262k / 32k | yes, 28.8 s; first attempt died at startup with `Error: Unexpected error database is locked` when launched concurrently with another opencode | same | same | backup |
| opencode | `opencode/nemotron-3.5-lightning-free` | 0 cost (NVIDIA trial endpoint, logged) | same | 262k / 262k | yes but 145.7 s wall for 1 tool call | same | same | too slow |
| opencode | `opencode/nemotron-3-ultra-free` | 0 cost (NVIDIA trial endpoint) | same | 1M / 128k | yes but 162.5 s wall | same | same | too slow |
| opencode | `opencode/mimo-v2.5-free` | 0 cost | same | 200k / 32k | NO: `write` executed but to a hallucinated path (`<user temp>/...`), hello.txt missing in cwd, 66.4 s | same | same | reject |
| cline 3.0.61 | provider `cline`, free list from `api.cline.bot/api/v1/ai/cline/recommended-models`: `cline-free/muse-spark-1.3-contributor`, `deepseek/deepseek-v4-flash`, `z-ai/glm-5.3-flash`, `cline-free/solar-pro4`, `cline-free/longcat-2.0`, `poolside/laguna-s-2.1:free` | free promo "up to a limited usage quota" (undocumented), needs a Cline account | undocumented | deepseek-v4-flash 1M, others 128k-262k (OpenRouter catalog) | NOT RUN: `{"type":"error","message":"Unauthorized: Please make sure you're using the latest version of Cline and re-authenticate your Cline account."}` (saved token in `~/.cline/data/settings/providers.json` is invalid; fix = owner runs `cline auth cline`, browser OAuth) | `cline --json --auto-approve true -c <dir> -P cline -m <model> "<prompt>"` | `run_result.usage{inputTokens,outputTokens,cacheReadTokens,cacheWriteTokens,totalCost}` | blocked on login |
| cline via Zen | `-P openai` + baseurl `https://opencode.ai/zen/v1`, model `big-pickle` (isolated `--data-dir`) | would be 0 cost | same as Zen | 200k | NO: `Invalid API key.` (Zen direct API needs a Zen key even for free models; opencode itself gets them without a key) | n/a | same | blocked: needs Zen account key |
| OpenRouter (through opencode or cline) | 19 `:free` models with tool support, e.g. `nvidia/nemotron-3.5-lightning:free` (1M), `thinkingmachines/inkling:free` (1M), `google/gemma-4-31b-it:free`, `poolside/laguna-s-2.1:free` | 0 cost per token | 20 RPM; 50 RPD with no credits, 1000 RPD once >= 10 USD credits bought (constants `FREE_MODEL_RATE_LIMIT_RPM=20`, `FREE_MODEL_NO_CREDITS_RPD=50`, `FREE_MODEL_HAS_CREDITS_RPD=1000` on openrouter.ai/docs/api-reference/limits) | 256k-1M | NOT RUN: no OpenRouter key configured anywhere on this machine | `opencode run -m openrouter/<id>` after `opencode providers login` | same as opencode | blocked; 50 RPD is 1-2 agent turns, useless without credits |
| agy 1.2.1 | `gemini-3.8-flash-medium` | included in owner's Google AI plan, not 0 cost; AI credits balance = 0 | opaque "work done" units, 5 h window + weekly cap; agy users report `RESOURCE_EXHAUSTED / Individual quota reached` with ~167 h reset after small sessions (antigravity-cli issues 37, 56, 215, 234); no CLI quota command, only the IDE panel (Agent Manager > Settings > Models) | 1M (Gemini) | yes but 13 steps and 53k input tokens for a trivial task, 40.7 s; without `--add-dir` it wrote `~/.gemini/antigravity-cli/scratch/hello.txt` (its own scratch), not the cwd | `agy --print "<prompt>" --output-format stream-json --model gemini-3.8-flash-medium --mode accept-edits --print-timeout 170s --add-dir <dir>` | `result.result.usage{input_tokens,output_tokens,thinking_tokens,cache_read_tokens,total_tokens}`; per-step in `step_update.usage` | usable but risks a week-long lockout of the owner's account |
| agy | `claude-sonnet-4-6` (also lists `claude-opus-4-6-thinking`, `gpt-oss-120b-medium`, gemini 3.1-pro/3.6/3.7/3.8 flash) | included in plan (third-party models = AI Pro/Ultra) | same pool | 1M | yes, 20.9 s, hello.txt = `ok` exact with `--add-dir` | same with `--model claude-sonnet-4-6` | same | reference agent only |
| gemini 0.59.0 | `gemini-3.8-flash` (result stats show `gemini-3.5-flash` served) | NOT free: project `Gemini Project 2` is Tier 1 with billing; Code Assist free tier for individuals (60 RPM / 1000 RPD) was shut down 2026-06-18 | Tier 1 paid limits; spend cap 200 EUR/month set in AI Studio | 1M | yes, 1 `write_file`, hello.txt = `ok`, but 74.7 s wall (about 65 s CLI startup: skills, MCPs, 18-36k input tokens per call) | `gemini -m gemini-3.8-flash -p "<prompt>" -o stream-json --yolo` | `result.stats{total_tokens,input_tokens,output_tokens,cached,tool_calls,models{<id>:{...}}}` | do not use: every call is billed |
| codex 0.154.0 | default (ChatGPT plan) | paid plan, 5 h + weekly windows | plan windows | 400k | yes, 34.6 s, hello.txt = `ok.` | `codex exec --json -s workspace-write --skip-git-repo-check -C <dir> "<prompt>" </dev/null` (`--full-auto` no longer exists; refuses untrusted dirs without `--skip-git-repo-check`) | `turn.completed.usage{input_tokens,cached_input_tokens,cache_write_input_tokens,output_tokens,reasoning_output_tokens}` | reference only |
| claude 2.1.263 | `haiku` (`claude-haiku-4-5-20251001`) | paid plan; `rate_limit_event`: five_hour utilization 0.62, seven_day 0.19, `overageStatus: rejected`, `overageDisabledReason: out_of_credits` | plan windows | 200k | yes, 16.4 s, hello.txt = `ok.`, cost 0.044 USD | `claude -p "<prompt>" --output-format stream-json --verbose --dangerously-skip-permissions --model haiku --max-turns 4` | `result.usage{input_tokens,cache_creation_input_tokens,cache_read_input_tokens,output_tokens}`, `result.total_cost_usd` | reference agent only (the paid frontier side of the pitch) |
| copilot 1.0.83 | `auto` | paid (AI credits since 2026-06-01; premium-request multipliers are legacy) | n/a | n/a | NOT RUN: `Error: No authentication information found.` (needs `copilot` then `/login`, or `COPILOT_GITHUB_TOKEN`) | `copilot -p "<prompt>" --allow-all --output-format json --model auto` | unknown | blocked on login |

## Throughput probe (5 identical calls in parallel, background processes)

| model | succeeded | total wall | per-call wall | 429 / rate-limit text |
|---|---|---|---|---|
| `opencode/big-pickle` | 5/5, hello.txt present in all 5 | 24.1 s | 16.9-23.1 s | none |
| `opencode/muse-spark-1.3-contributor-free` | 5/5, hello.txt = `ok` in all 5 | 25.7 s | 18.3-24.6 s | none |

Logs: `<SCRATCH>/providers/probe-big-pickle-{1..5}/`, `<SCRATCH>/providers/probe-muse-{1..5}/`.
`opencode stats` after all runs: 17 sessions, 232.3K input / 2.6K output / 270.0K cache read, Total Cost $0.00.

## Recommendation

Workhorse: opencode with `opencode/big-pickle`. Runner-up: opencode with
`opencode/muse-spark-1.3-contributor-free`.

1. Only opencode gives a working free tool loop today with zero login: Zen free models need no key, cline's token is dead, copilot is logged out, gemini bills every call, agy burns a subscription quota that can lock the owner out for a week.
2. big-pickle is the fastest free model measured (2.9 s model time, ~14-18 s wall including opencode startup) and passed 5/5 in parallel; muse-spark is nearly as fast, follows the content spec exactly, and has a 1M context (big-pickle: 200k, matters for 0.3-1M-token turns, so prefer muse-spark for long turns).
3. The nemotron models work but take 145-162 s per tool call; mimo writes to the wrong path.
4. Expected cost of one 20-agent run: 0 EUR (every Zen free model reports cost 0; `opencode stats` total $0.00 after 17 sessions). The unknown is the per-IP daily free quota: it is undocumented and resets at 00:00 UTC, and a 20 x 0.3-1M-token run may exhaust it mid-run with `429 FreeUsageLimitError`. Mitigation: spread agents over big-pickle, muse-spark and ling (limits are per model per IP according to issue 42977), and treat a 429 as "wait for 00:00 UTC".
5. Second unknown: the opencode local SQLite (`~/.local/share/opencode/opencode.db`) threw `database is locked` once when two processes started at the same moment; stagger launches by ~1 s or run one `opencode serve` and use `opencode run --attach http://localhost:<port>`.

## AI Studio spend line

https://aistudio.google.com/spend (project "Gemini Project 2", `gen-lang-client-0836667126`), page in French, currency EUR:
"Niveau 1" (Tier 1, billing enabled). "Plafond de depenses mensuel: 71,54 EUR / 200,00 EUR".
"Votre cout total, August 18 - September 14, 2026: Cout EUR 74.24 - Economies EUR 37.42 = Cout total EUR 36.83".
Billing page (`/billing`): "Payant 1 - Plafond du niveau du compte de facturation : 214,55 EUR", September 1-13, 2026: Cout EUR 71.30 - Economies EUR 34.47 = Cout total EUR 36.83. No free credit line anywhere; it is a post-paid account.
Google One AI credits (https://one.google.com/ai/activity): "Credits d'IA: 0"; last Antigravity credit debits 17 June 2026 (-797, -188); note "Les credits d'IA inclus dans votre forfait ont ete remplaces par des limites d'utilisation basees sur les produits".

## Raw evidence

- CLI help captures: `<SCRATCH>/providers/help/*.txt` (cline, opencode-run, opencode-models, opencode-providers, agy, agy-models, gemini, codex, claude, copilot).
- Catalogs: `<SCRATCH>/providers/web/cline-recommended.json`, `opencode-zen-models.json`, `modelsdev.json` (opencode provider block has context/cost per model), `openrouter-models.json`, `openrouter-limits.html`.
- Smoke runs (argv.txt, out.txt, err.txt, result.txt): `<SCRATCH>/providers/opencode-big-pickle/`, `opencode-muse-spark-1.3-contributor-free/`, `opencode-ling-3.0-flash-fin-free/`, `opencode-nemotron-3.5-lightning-free/`, `opencode-nemotron-3-ultra-free/`, `opencode-mimo-v2.5-free/`, `cline-default/`, `cline-zen-big-pickle/`, `agy-gemini-3.8-flash-medium/`, `agy-claude-sonnet-4-6/`, `gemini-default/` (plain "say ok"), `gemini-3.8-flash/`, `codex-default/`, `claude-haiku/`, `copilot-auto/`.
- Event samples (one line per type, truncated): run `node <SCRATCH>/providers/events.js <dir>/out.txt`. opencode: `step_start`, `tool_use`, `step_finish`, `text`. agy: `init`, `step_update` (user_input / agent_response / tool with tool_name, tool_info.parameters, usage), `result`. gemini: `init`, `message`, `tool_use`, `tool_result`, `result`. cline: `hook_event`, `agent_event`, `run_result`. codex: `thread.started`, `turn.started`, `item.started`, `item.completed`, `turn.completed`. claude: `system`, `assistant`, `user`, `rate_limit_event`, `result`.
- Web sources: opencode.ai/docs/zen (free list, "limited time", privacy/logging terms), github.com/anomalyco/opencode issues 15714/42765/42977/10404 (quota behaviour), docs.cline.bot/getting-started/free-models and /clinepass, api.cline.bot recommended-models endpoint, openrouter.ai/docs/api-reference/limits, antigravity.google/docs/plans, github.com/google-antigravity/antigravity-cli issue 37, developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals, ai.google.dev/gemini-api/docs/rate-limits (usage tiers), docs.github.com copilot supported-models and copilot-requests (legacy).

# Update 2026-09-15: every free CLI gateway from the table above went dark

The 2026-09-14 scaling sweep (sizes 1, 2, 5, 10, 20 on `prompts/18-secure-server.json`)
produced five failures in 2h21 and no measurement. Cause, read back from
`adws/adw_data/sessions/<run>/*/events.jsonl` and `stderr.log`:

| row | what the log holds | reading |
|---|---|---|
| size 2, run `648c93fe` | prototype OK (515k tokens), then every owner and reviewer `opencode exit=1`, 83KB of events ending mid tool call | Zen dropped the connection in the middle of a turn |
| sizes 5 / 10 / 20 | `events.jsonl` 0 bytes, `stderr.log` 0 bytes, exit 1 after ~5 s, both prototype attempts | Zen answered nothing at all |

Re-measured the same night (2026-09-15, 00:00-01:20 local):

| provider | model | verdict | evidence |
|---|---|---|---|
| opencode Zen | `opencode/big-pickle` | DEAD | probe hung with 0 bytes on stdout AND stderr until killed at 8 min |
| opencode Zen | `opencode/muse-spark-1.3-contributor-free` | DEAD | same, 0 bytes, killed |
| cline | `deepseek/deepseek-v4-flash` | CAPPED | `429 INFERENCE_CAP_ERROR ... Daily free limit reached ... Try again in 12h 9m` |
| kilo | `kilo/poolside/laguna-s-2.1:free` | ALIVE, slow | `check_runner.py` PASS, 33.8k tokens, but 256 s for one trivial file write |
| kilo | `kilo/kilo-auto/free` | ALIVE | `check_runner.py` PASS |

So the workhorse row at the top of this file is stale: `opencode/big-pickle` is not a
workhorse, it is a provider that fails silently. That is why the swarm now has an HTTP
runner of its own and does not depend on any coding-agent CLI.

## NVIDIA NIM direct (`adws/adw_modules/runners/nim.py`, new)

`https://integrate.api.nvidia.com/v1`, OpenAI-compatible, key in `.env` as `NVIDIA_API_KEY`.
`/v1/models` answers in 1.3 s and lists 81 models. One tool-calling probe each,
`bench/nim_models.py` (re-runnable):

| model | verdict | wall | first token | note |
|---|---|---|---|---|
| `nvidia/nemotron-3.5-lightning-30b-a3b` | TOOL | 2.4 s | 1.6 s | fastest |
| `nvidia/nemotron-3-super-120b-a12b` | TOOL | 3.2 s | 2.9 s | WORKHORSE |
| `openai/gpt-oss-20b` | TOOL | 4.6 s | 2.9 s | |
| `z-ai/glm-5.3-flash` | TOOL | 10.0 s | 3.4 s | |
| `moonshotai/kimi-k3` | TOOL | 326.2 s | 319.5 s | answers, but see below |
| `deepseek-ai/deepseek-v4-flash-0731` | timeout | 242 s | - | never produced a byte |
| `mistralai/mistral-nemotron` | TEXT | 32.5 s | 31.7 s | ignored the tool, answered in SQL |
| `moonshotai/kimi-k2.6`, `nvidia/nemotron-nano-3-30b-a3b` | HTTP 404 | - | - | listed but not served on this account |
| `poolside/laguna-xs-2.1` | EMPTY | 0.3 s | 0.3 s | stream closed with no content |

**kimi-k3 is not usable for a swarm on this account.** It does support tool calling, but
its queue wait (319 s to first token, measured) is longer than the NVIDIA gateway's own
patience: `bench/check_runner.py nim moonshotai/kimi-k3` came back `HTTP 504` at 303.7 s.
The one probe that got through took 326 s for a single 290-token answer. Twenty agents
times two rounds at five minutes a call, with a 504 whenever the queue is a little longer,
is not a measurable swarm.

`nvidia/nemotron-3-super-120b-a12b` runs the same probe in 18.6 s end to end through the
full runner (`check_runner.py` PASS, 4,228 tokens, 2 tool calls), and
`nvidia/nemotron-3.5-lightning-30b-a3b` in 34.6 s with 3 tool calls. Both are free.

## 2026-09-16, afternoon: where the three swarm models stand on public numbers

What the September write-ups say about the models the ladders run on, so the choice of
roles is not folklore (sources: benchlm.ai model page, regolo.ai tier comparison 09-04,
Medium 09-10, Command Code 09-11, idapt price table):

| model | agentic coding | speed | list $/M in / out | note |
|---|---|---|---|---|
| deepseek-v4.1-flash | Terminal-Bench 2.1 90.6 (top of the board, closed models included), DeepSWE 74.2 | ~108 tok/s (V4 Flash first-party) | 0.44 / 1.32 peak, halved off-peak, cache hits ~0.007 | beta: 429s and a 20-request concurrency ceiling reported; open weights not confirmed |
| glm-5.3-flash | DeepSWE 63.4, Terminal-Bench 2.1 84.3, AA index 57 (3 below the full GLM 5.3 at a tenth of the tokens) | 48.7 tok/s | 0.15 / 0.50 | MIT weights; the tier's best quality per dollar on async agent work per regolo |
| muse-spark-1.3 | Surge Work Index leads the open field on several columns; no DeepSWE figure found | n.r. | 1.25 / 4.25 | free as "contributor" on Zen and cline (data goes to Meta, geo-banned outside a few regions) |
| kimi-k3 | DeepSWE 67.5, best long-horizon (SWE Marathon, ProgramBench) | queue on NIM | 3 / 15 | out: the NIM queue (S above), the price |
| qwen3.8-flash-next | DeepSWE 58.7, SWE-bench Pro 62.5 | 88 tok/s | 0.15 / 0.47 | not on our free endpoints yet; the candidate to add if cline lists it |

What this changes here: nothing in the roles, everything in the confidence. The two models
the ladders put in the whole-file seats (muse-spark on Zen, deepseek-v4.1-flash on Token
Harbor) and the one in the owner seats (glm-5.3-flash on cline) are the three the public
numbers also put first in their price class; deepseek's "additive" style (seven appends,
then a stop) is the one to watch, and the harness now pushes back on it (F14).
