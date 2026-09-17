# Frictions, what the swarm lacks, and where it stands against Dan's (2026-09-16)

Three days, 60-odd runs, four providers. Each line below is something a run actually did, with
the fix or the gap that remains.

## Frictions met, in the order they cost time

| # | friction | seen in | cost | state |
|---|---|---|---|---|
| F1 | a dead free gateway answers `exit=1` with no text; the sweep ran 2h21 producing five failures before anyone looked | opencode Zen, 09-14 night | one night | fixed: every exit names its reason; `scale_sweep.sh` preflights each model before size 1 |
| F2 | per-minute rate limit hit by five owners cycling tool calls; 39 x 429, 180 x 503 in one night | NIM | round 1 unusable | fixed: process-wide adaptive spacing, shared cooldown, retry ladder; then found the limit is per model and the good model is the congested one |
| F3 | overwrite-only `write_file`: 14 KB body, 1.3 KB block over it, 60 times | NIM solo | 20 min, empty file | fixed: `edit_file`, `append`, whole-file `read_file`, REPLACED notice |
| F4 | the model narrates instead of acting: "wrote server.js", zero tool calls, twice | NIM prototype, finisher | a whole size | fixed: nudge twice, then an explicit failure; `tool_choice: required` is garbage on NIM |
| F5 | `node server.js` in the foreground: shell killed, pipe held, `communicate()` waits an hour; the harness had no deadline for an in-process agent | NIM size 1 | 76 min | fixed: process-tree kill, abandonable pipe readers, `peer_round` abandons a future past `timeout + 120 s` |
| F6 | the contract names 16 owners, the reduced roster says two; the model refuses as contradictory | NIM size 2 | a prototype | fixed: reduced-swarm note in the brief |
| F7 | the model writes HTML entities decoded (`&amp;` -> `&`) unless warned; every configuration, solo or swarm, capped at 18/19 on the same test | NIM, all runs | the whole first ladder read as a scaling law | fixed: caution on the writing tools; the 19th test then passed for every cline configuration |
| F8 | cline's daily cap per model: 13:00 reset, one 20-agent run per model per day | cline | size 20 twice, the treatment arm | mitigated: owners on the model with the freshest bucket, whole-file on another; not fixable on the free tier |
| F9 | envelope JSON the harness cannot parse (deepseek, glm: unescaped quotes in a long summary) | cline, ~1 in 5 owners | agent marked failed, block still salvaged from disk | fixed: `_extract_json` salvages status, summary and code from an envelope whose strings hold a quote (`test_an_envelope_with_an_unescaped_quote_keeps_its_status_and_summary`) |
| F10 | test servers left running by agents: 16 `node server.js` after two ladders | cline, host | ports, memory | fixed on the host: every CLI and every `run_command` runs in a Windows job object that dies with the agent's turn (`spawn`/`new_job`/`terminate`; two tests spawn a 60 s child and see it gone); the container stays the answer for isolation |
| F11 | the inbox delivered the same message twice: cursor stored as a formatted float | coord tools | silent | fixed: per-post cursor by filename |
| F12 | cline's tool events carry no command text, so `measure_run` read `coord 0` next to `done 2` | treatment arm | wrong metric | fixed: `swarm.py inbox` logs itself to the board history |
| F13 | the fixed suite is green while 14 of 16 panels are empty, or the New button throws | big-pickle solo, kanban and notebook | the whole comparison was blind to it | fixed today: `bench/ui_gate.py`; not yet in the acceptance gate |
| F14 | the model narrates its next step ("Now swimlanes, assignees, exporter...") after seven appends and stops; the file ends mid-function, the turn has no envelope, the harness takes the narration as the final answer | deepseek-v4.1-flash solo, kanban | a whole solo, 234k tokens | fixed: a text-only stop after tool work with no JSON in it is pushed back with `UNFINISHED` (`test_a_narrated_next_step_after_tool_work_is_pushed_back`) |
| F15 | the repair turn on that truncated file spent 25 tool calls and 604k tokens writing inspect scripts (inspect.py to inspect6.py, harness.py) to discover the script tag was never closed, then ran out of time without one edit | deepseek-v4.1-flash repair, kanban | 30 min, one repair round | fixed: `solo_first.shape_defects` names a truncated file, an unclosed script and a missing app block before any test, first in the defect list; a repair that scores lower than the kept page is discarded (S17) |
| F16 | the 20-feature spreadsheet brief (2,000 lines) does not fit the nim loop's 60-step cap when the model builds by edits: deepseek-v4.1-flash made 36 edit_file and 24 run_command calls, 3.3M tokens (3.2M of them input, the history re-sent every step, no cache credit from Token Harbor), and stopped on a syntax error with no envelope; $1.5 at list price for a red page | deepseek-v4.1-flash solo, spec 20 | a whole solo | mitigated: `SWARM_NIM_MAX_STEPS` (120 for spec 20); open: the input growth is the cost driver, a cached-prefix provider or a stricter re-read discipline is the fix |
| F17 | muse-spark on cline answers the same brief with "The operation timed out." after eleven minutes of reasoning: cline's own request deadline, not ours; on Zen the same solo got 0 bytes for 32 minutes before Zen died | muse-spark solo, spec 20 | two solos | mitigated: `SWARM_CLINE_THINKING` (medium) shortens the reasoning; open: a brief this size is where the parts pipeline earns its place for a whole-file model |
| F18 | the browser gate crashed (a missing `import os` after an edit) and `solo_first` read `ok: None` as not-red: a 22/22 page with 6 empty panels and 5/17 clicks was declared green | muse-spark solo, spec 20 | one false green | fixed: a gate error is red; the page was resumed into the repair loop (`--resume`) |
| F19 | three free providers out in one afternoon: Zen dead at 12:16 (0 bytes, exit 143), cline's muse-spark daily cap after a 29M-token solo ("Try again in 23h 14m"), Token Harbor's free allowance for the week gone at 13:59 (`free_tier_limit_reached`, next period 2026-09-23, `Retry-After: 3600` which the runner honoured and sat on) | every spec-20 run | the deepseek ladder, the muse repairs 2 and 3, the cline arm sizes 10 and 20 | mitigated: the ladder moved to cline glm-5.3-flash, the one model with quota left; fixed: a `Retry-After` above `SWARM_NIM_MAX_RETRY_WAIT` (600 s) fails the call at once with the provider's own words (`test_a_retry_after_beyond_the_budget_fails_the_call_at_once`) |
| F20 | cline cuts a stream at about twelve minutes whatever the model: glm-5.3-flash's prototype turn on spec 20 reasoned 743 s and ended with "Response stream ended without a finish reason" (70k in, 6k out, nothing written); the second try was still reasoning 18 minutes later | glm prototype, spec 20 ladder on cline | the first size of the ladder, 30 min | mitigated: the ladder relaunched with `SWARM_CLINE_THINKING=medium`; the spec-20 brief is where a whole-file turn has to be short-thinking or split |
| F21 | one reply longer than the 16k output cap ends the whole turn: nemotron-3-super had 1,920 lines of spec 20 on disk (8/22 tests) when a single over-long message lost it the envelope | nemotron solo, spec 20 | the solo's verdict (the repair loop caught it) | fixed: a `length` finish is pushed back with `CUT_OFF` (the disk is intact, continue in 120-line calls), fatal only after `NUDGES` (`test_a_reply_cut_at_the_output_cap_is_pushed_back_not_fatal`) |
| F22 | one repair turn on a 1,900-line page cost 7.2M tokens: the model re-read the whole file (16k tokens) at every step and the history re-sent it all; $1.4 at nemotron's list price for 14 -> 8 failing tests | nemotron repair, spec 20 | the cost of the repair loop on big files | mitigated: `read_file` takes a `lines` range (numbered, matched to edit_file) and the repair brief says grep first; open: prompt caching is the real fix and no free gateway credits it |

## What the swarm lacks, measured, against the same models alone

- **A reason to exist on these briefs.** Solo muse-spark: 19/19 in 3 min ($1.81 list), 18/18 in
  3.7 min ($1.33). Every swarm size lands the same score for 2-11x the tokens and 4-20x the time.
  The one measurable product is variance reduction (S10): a solo that ships a shell (big-pickle,
  kanban) or a crash (big-pickle, notebook) versus a swarm whose assembly works. That is worth
  paying for only when the soloist is the cheap model and the gate is red.
- **Cohesion in the cut.** The block list is the feature list. Three owners on the secure brief
  found the same frozen-region defect and could not touch it; the finisher had to.
- **A judge for the page.** Until today nothing in the pipeline opened the page. Now `ui_gate`
  does, outside the gate; it needs to move inside it and feed the finisher.
- **A container.** Dan's first rule. Docker Desktop is installed and off; the runner routes
  `run_command` through the sandbox when one exists, unverified. On the host, every agent turn
  now runs in a job object that dies with it (F10).
- **A brief that needs it, measured (afternoon).** Spec 20 is the first: 2,000 lines, a
  whole-file turn that no free model finishes in one stream (cline cuts at ~12 min, F17/F20;
  the nim loop's step cap, F16), and a soloist that is red on the page (5/17) until a repair
  turn (15/17). What the swarm has to prove there is that owners writing 100-line blocks reach
  17/17 for fewer tokens than a soloist plus repairs (muse: 29M + 6.6M). The cline glm ladder is
  running that measurement; three of four free providers died before it could.

## Against Dan's swarm (video S2sjyokoxeE), item by item

| Dan | ours | verdict |
|---|---|---|
| agents in containers | host, Docker off | missing |
| tools the agent calls: `inbox`, `list_team`, `claims`, `file_history`, `claim_file(seconds)`, `release_file`, `done` | all eight in `board/swarm.py`, off by default (`SWARM_COORD=1`); used in the two treatment runs that ran (2 `done`, inbox read by both owners) | present, not yet default, not yet measured at size 5+ |
| open-ended session until the agent says done | up to `SWARM_OWNER_TURNS` (3) turns per owner while there is a reason: no block and no `done`, or a direct @question in its inbox it never read (`coord.unread`); a broadcast reopens nothing | present, capped |
| one thread, tagged messages | one `thread.md`, `@agent` tags, per-agent inbox cursor | present |
| shared budget visible to agents | `budget.json` with tokens and dollars, `budget.py` | present |
| a coordinator's view of who holds what | `team`, `claims`, `history` | present |
| the harness pushes nothing, the agent pulls | the brief still carries the mailbox snapshot; the tools add the pull | both |
| one model per swarm, role prompts | two models (whole-file, owners) plus per-role reviewers | different, deliberate: the output cap made it necessary |

What "reaching Dan's level" still requires: the container on (Docker off on this machine; the
host now has the job-object kill instead), and the coordination tools on by default once the
treatment ladders (spec 18 on cline, spec 19 on Zen) are measured. The open session landed on
2026-09-16 afternoon, capped at three turns. Everything else in his demo is here and measured.
