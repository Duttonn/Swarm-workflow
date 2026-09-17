# Free-model swarms against one Opus agent, project-sized briefs (2026-09-14)

Two new briefs, each a self-contained one-page web app with 12 user-visible features and 14 fixed
tests (12 `test_feature_*`, 2 structural): `prompts/16-budget-ledger.json` and
`prompts/17-markdown-notebook.json`. Every row below was judged with the same suite by the harness
or by `bench/solo_run.py`; wall times are on one laptop (16 cores, 15 GB) that was also running the
other rows at the time noted. Free models cost 0 by their gateways' own accounting (`usage.json`,
`cost_usd: 0`; `opencode stats` total $0.00).

## Spec 16, budget ledger

| agent(s) | features | tests | wall | tokens | USD | note |
|---|---|---|---|---|---|---|
| 1 x Claude Opus 5 | 12/12 | 14/14 | 3 min 35 s | 0.54M | 1.10 | 6 API calls |
| 1 x big-pickle (opencode Zen, free) | 12/12 | 14/14 | 21 min 41 s | 0.65M | 0 | 8 tool calls, 41 KB |
| 20 x big-pickle swarm, run `4695d87e` | 12/12 | 14/14 (assembled) | ~54 min | 11.2M | 0 | 16 blocks, 20 reviews, 7 defects; the run died at the finisher on `WinError 1450` (two swarms on the laptop) |
| 1 x DeepSeek V4 Flash (cline, free) | 0 | - | 7 min 32 s | - | 0 | `maximum output token limit`, no file |
| 20 x DeepSeek V4 Flash swarm, run `e1deb9d1` | - | refused | 54 min | 6.5M | 0 | prototype hit the output cap twice, 3 blocks salvaged |
| 20 x DeepSeek V4 Flash parts + muse-spark prototype/finisher, run `df4d8ef1` | - | died | 20 min | - | 0 | prototype cut 16 blocks first try, 8 owners done, then `WinError 1450` from a `stat()` in the board poll (harness bug, fixed) |
| same, run `4121c220` | 12/12 | 14/14 (assembled and finished, gate passed) | 40 min 53 s | 10.5M | 0 | 16 blocks first try; the 12 feature blocks written by DeepSeek owners; the 4 infrastructure owners (store, router, styles, shell) and all 20 reviewers hit cline's `429 Daily free limit reached on deepseek-v4-flash, try again in 19h 58m`, so those 4 blocks stayed as the muse-spark prototype wrote them and no review ran; the session row says `fail` because a whole round died, the gate says pass |

## Spec 17, markdown notebook

| agent(s) | features | tests | wall | tokens | USD | note |
|---|---|---|---|---|---|---|
| 1 x Claude Opus 5 | 12/12 | 14/14 | 6 min 41 s | 0.49M | 2.03 | 5 API calls |
| 1 x big-pickle (free) | 12/12 | 14/14 | 3 min 14 s | 0.33M | 0 | 10 tool calls, 28 KB |
| 20 x big-pickle swarm, run `89d33713` | - | killed | - | 0.2M | 0 | prototype hit big-pickle's 32k output cap (`reason: length`) before writing anything |
| 20 x swarm, muse-spark prototype/finisher + big-pickle parts, run `b778d291` | 12/12 | 14/14 | 48 min 13 s | 11.1M | 0 | accepted: 16 blocks first try, 15/16 owners (tags salvaged from disk), 20/20 reviews, 11 defects, finisher passed |

## What this says

1. On a 12-feature single-file brief, one free model delivers 12/12, the same as Opus 5, at zero cost;
   on spec 17 it was even faster than Opus. The "one agent forgets 8 of 12 features" premise did not
   show up at this size for any model that could write the file at all.
2. The free swarm also delivers 12/12 at zero cost, for 17-34x the tokens and 7-15x the wall time of
   the free soloist. Its measurable extras are the 20 independent reviews (7 and 11 defects listed and
   applied) and the per-block ownership; neither moved the test score here.
3. Output caps, not intelligence, decide who can build alone: DeepSeek V4 Flash and big-pickle (32k
   output per turn, reasoning included) both died writing a 40 KB file in one go. The swarm survives
   this only if the two whole-file turns (prototype, finisher) run on a large-output model
   (`SWARM_WHOLE_FILE_MODEL` / `SWARM_WHOLE_FILE_RUNNER`, muse-spark at 131k output); the 16 owners
   then write ~1-3 KB each on whatever model. Run `4121c220` did exactly that: DeepSeek V4 Flash, 0/12 alone,
   wrote the 12 feature blocks that passed 12/12 inside the swarm.
4. The harness now takes deliverables from disk (`delivered_file`): an agent cut off after writing its
   file still counts. Runs `4695d87e` and `b778d291` both salvaged a block that way.

## Frictions found and what changed

| # | friction | evidence | change |
|---|---|---|---|
| 1 | whole file returned inside the json reply hits output caps | d06d9c1f try-1 `reason: length` at 32,682 output tokens; try-2 1.6M tokens then exit 1 | prototype, owners and finisher deliver by writing the file; harness reads disk first, `code` is the fallback |
| 2 | one write call with the whole file also hits the cap | e1deb9d1 (DeepSeek) `maximum output token limit`; 89d33713 (big-pickle) 32,757 output tokens, no file | prompts demand skeleton first then per-block edits, <=120 lines per call; per-stage model override for the two whole-file turns |
| 3 | a transient `WinError 1450` on the board directory killed whole runs | 4695d87e at the finisher after 16/16 parts and 20/20 reviews; df4d8ef1 after 8/16 parts, from `p.stat()` in `board_posts`' sort key | every filesystem call in `board_posts` is guarded and the owner-thread poll never propagates an OSError; one swarm at a time, `SWARM_MAX_PARALLEL=4` |
| 4 | json envelope breaks on a stray character | e1deb9d1 `styles: Expecting ',' delimiter`; b778d291 `tags: no JSON object found` | block salvaged from the verified block file, no turn lost |
| 5 | cline tool calls were invisible to the monitor | check_runner `tool_events=0` | runner maps `agent_event.contentType == "tool"` per `toolCallId` |
| 6 | review is the slowest stage | b778d291: build 1138 s, review 1426 s, finish 77 s at 4 parallel | not changed; candidates: reviewers only, or 8 parallel on a bigger machine |
| 7 | `measure_run.py` reads only the legacy Gemini token layout | stage tokens print 0 for usage.json runs | not changed; the run total in the db is right |
| 8 | cline's free tier has a small per-model daily cap | 4121c220: `429 Daily free limit reached on model deepseek/deepseek-v4-flash-0731, try again in 19h 58m` after ~13 agent turns plus one solo and one probe | not changed; spread a swarm over several free models (glm-5.3-flash, solar-pro4, longcat-2.0, laguna) or keep cline for the small stages; opencode Zen and Kilo showed no cap over 40+ turns |

## Providers

`bench/providers-2026-09-14.md` has the survey. Verified through `bench/check_runner.py` on 2026-09-14:
opencode `big-pickle`, `muse-spark-1.3-contributor-free`, `ling-3.0-flash-fin-free`; kilo
`poolside/laguna-s-2.1:free`, `kilo-auto/free`, `stepfun/step-3.7-flash:free` (`nex-n2.5-pro:free` timed
out at 250 s); cline `deepseek/deepseek-v4-flash` (after `cline auth cline`); agy, gemini (billed), claude
haiku, codex (paid plans). Three free gateways, three separate quotas.
