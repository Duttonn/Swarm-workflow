# What the pages do when a person clicks them (2026-09-16)

The fixed suites judge pure model functions in node. This is the other half: every
deliverable served from `work/webtest/` on `http://localhost:8765`, driven in the app's Browser
pane with one scenario per brief (`work/webtest/probe-*.js`, run through the pane's JS tool),
then inspected by hand wherever the scenario and the page disagreed on the shape of a control.
A step is what a user does and what they then see. "Probe" in a note means the scenario's
guess at the control was wrong and a hand check settled it.

## Budget ledger (spec 16, 12 features): add, refuse, balance, categories, search, sort, export, import, persist

| build | scenario | hand check |
|---|---|---|
| Opus 5 | 9/9 | - |
| big-pickle alone (Zen) | 9/9 | - |
| 20-agent swarm `4121c220` (DeepSeek owners) | 9/9 | export puts the CSV in a textarea's `.value` (probe first read text nodes) |

No difference a user would feel. No console error on any of the three.

## Markdown notebook (spec 17, 12 features): create two notes, preview, tags, links, backlinks, search, outline, stats, pins, undo, export, persist

| build | verdict |
|---|---|
| Opus 5 | 13/13 |
| big-pickle alone (Zen) | **unusable**: `+ New Note` throws `Cannot read properties of null (reading 'past')` on a fresh page; the store starts with `history: null` and the click handler reads `.past`, so no note can ever be created. Its 14/14 on the fixed suite is a pure-function score; the wiring that a user touches was never run. Also asks the title through `prompt()`. |
| 20-agent swarm `b778d291` (big-pickle owners, muse-spark whole file) | works by hand: type in the "New note" form, Add, click the note, Preview renders `<h1>`, `<strong>`, `<ul>`; two forms on one panel ("New note" + "Editing") is clumsy but functional (scenario 10/13, the misses were the probe choosing the wrong form) |

This is the one place in three days where the swarm's assembly beat the same model working
alone at something a user would notice, and the reason is plain: the soloist never opened its
own page, the swarm's finisher and reviewers ran the file.

## Kanban board (spec 19, 16 features, new today): tabs, add column, refuse blank, add card, refuse blank, move, WIP set + refusal, undo, export, import with an invalid card, metrics, persist

| build | scenario | hand check |
|---|---|---|
| Opus 5 (baseline) | 12/14 | the two misses are the probe: it pressed the first "to Doing" button, which belongs to another card |
| big-pickle alone (Zen), 18/18 on the suite in 9 min 48 s | 3/14 | **a shell**: 14 of the 16 panels render empty (Move, WIP, History, Export, Import, Metrics... no control at all), the board starts with no columns, a card added from the Cards form lands in column `''`. Screenshot kept in the session. Every model function passes; almost no render function draws anything. |
| muse-spark alone (Zen), 18/18 on the suite in 3 min 42 s | 7/14 | swimlanes, WIP (a Set button per row) and export (a Show JSON button) work by hand; **undo is not wired**: History reads "depth 0" after five changes, Undo does nothing; **import errors are not shown**: the invalid card is dropped silently, the valid one added; **Move is a form asking for a card id** (`card-0qzcxwho-2`) typed by hand, usable by a developer only |

Same pattern as the notebook, now at two grades: muse-spark cut corners on three panels,
big-pickle drew fourteen empty ones. Both suites are green. This is the brief where "one agent
forgets most of the features" is finally visible, and it is visible only in the browser: the
model functions the suite calls exist and pass; the render functions a person needs were never
written or never run.

## What this changes

- A green fixed suite is necessary, not sufficient. The next judge needs a browser step:
  load the page headless, click every tab, submit every form once, and fail on a console
  error or an unwired control. `swarm.py render` already opens a headless Edge; extending it
  from "does it draw" to "do the forms work" is the cheapest next gate.
- The swarm's structural advantage shows up exactly here, not in the test score: the
  finisher and the reviewers open the file, so a control that throws gets caught before
  shipping. A soloist that never runs its page ships whatever it typed.

## The gate, made repeatable (12:00)

`adws/adw_modules/ui_gate.py` is now in the pipeline: the assembled page is opened in headless
Edge before the finisher, its store is seeded through the contract, and the spec's own click
scenario (`ui_scenario`, new field, spec 19 carries one: 14 steps, add/refuse/move/WIP/undo/
redo/export/import/search/archive) runs on it. Every empty panel, mount error and failed step
becomes a defect at the top of the finisher's list; among test-passing candidates the one
whose page also works ships. Same judge, same scenario, on the four kanban builds:

| build | suite | panels | scenario steps | what a person cannot do |
|---|---|---|---|---|
| Opus 5 | 18/18 | 16/16 | 13/14 | the import error list (probe timing, checked by hand: shown) |
| muse-spark alone | 18/18 | 16/16 | 7/14 | move a card from the board, set a WIP limit, undo, export, see import errors, archive |
| big-pickle alone | 18/18 | 3/16 + a load error | 2/14 | almost everything |
| swarm size 1 (`9958cafc`, muse-spark prototype, big-pickle owner) | 18/18 | 16/16 | 8/14 | move from the board, WIP, undo, export, import errors |

The swarm ladder sizes 2 to 20 on this brief run with the gate feeding the finisher; that is
the test of whether twenty agents fix what one agent leaves unwired.

## Size 5 with the gate feeding the finisher (`b5fcb1a7`, 11:05)

| candidate | suite | panels | clicks | note |
|---|---|---|---|---|
| assembled (prototype + 5 owners) | 18/18 | 16/16 | 9/14 | the gate listed the five misses: move from the board, WIP refusal, undo, export, import errors |
| finished (one finisher, given those five as defects) | 18/18 | 16/16 | **14/14** | shipped: `usable: {assembled: false, finished: true}` |

21 min 30 s, 6.0M tokens. The first page in three days that a person can use end to end and
that no single free agent produced: muse-spark alone 7/14 in 3.7 min, big-pickle alone 2/14.
The mechanism is not twenty agents, it is a verifier that can see the page plus one agent told
exactly what it found. Whether one agent given the same list does as well as the swarm is the
next measurement (`bench/solo_first.py --repair`).

## Sizes 10 and 20 on Zen (`c25721b8` 11:05-11:36, `081c09a0` 11:36-12:16, then Zen died)

| size | suite | panels | clicks | minutes | tokens | note |
|---|---|---|---|---|---|---|
| 10 | 18/18 | 16/16 | 8/14 | 30.3 | 11.9M | the finisher got "scenario produced no result" from the gate (pre-timer-race code), so it fixed nothing the gate saw; the page hangs Edge's virtual clock on a re-gate, which is a defect of the page (a timer loop) the gate now reports as a timeout |
| 20 | 18/18 | 16/16 | 9/14 | 40 to the assembled page | 5.1M at that point | all 16 blocks landed by 12:16; the review round then got 0 bytes from Zen for 25 minutes per call: Zen died again (F1, 0 bytes and exit 143 on a direct probe at 13:05). Run stopped; the assembled candidate is the deliverable measured here |

The non-monotone shape (S12) again: 2 -> 11/14, 5 -> 14/14 with the gate feeding the finisher,
10 -> 8/14, 20 -> 9/14 before its finisher. More owners is more integration surface for the
finisher, and the finisher's one turn is the bottleneck, not the owners.

## The ablation: one agent plus the same gate (`bench/solo_first.py --repair`, 11:35-11:50)

Same soloist (muse-spark), same brief, the gate's list handed back to it as defects, up to
three turns on a copy of its page.

| step | clicks | minutes | tokens |
|---|---|---|---|
| solo | 8/14 | 4.2 | 0.53M |
| repair 1 (6 defects listed) | 10/14 | 2.9 | 0.95M |
| repair 2 (4 listed) | **14/14** | 5.0 | 1.42M |
| repair 3 | (spent on a false negative: the Move step did not know this page's card select; scenario widened, it reads 14/14) | 2.0 | 0.60M |

Solo + two repairs: 14/14 in 12 minutes for 2.9M tokens. Swarm size 5 with the gate feeding its
finisher: 14/14 in 21.5 minutes for 6.0M. Same page quality, half the time, half the tokens.
The verifier is the product; the swarm is a more expensive way to run the verifier loop, and
on this brief it never found something the loop did not.

Where the swarm still earns its place, measured: a model that cannot write the whole file alone
(DeepSeek V4 Flash, output cap) and a soloist whose page throws on load (big-pickle) - the
first needs the parts pipeline, the second needs any second agent at all. For a model that can
hold the file, solo + gate + repair is the policy.

## Spreadsheet (spec 20, 20 features, 22 tests, 17 clicks): the brief where the soloist is red

Built this afternoon as the next rung (`work/spec20/`, `prompts/20-spreadsheet.json`): a formula
parser and evaluator, cross-sheet references, named ranges, a dependency graph with cycles, six
error codes, reference rewriting on row and column insertion, fill series, CSV, an SVG chart,
validation, conditional formatting, undo fifty deep, a versioned store. The Opus baseline is 22/22
and 17/17 clicks. Zen died at 12:16, so the free models ran on cline (muse-spark) and Token
Harbor (deepseek-v4.1-flash).

| who | suite | panels | clicks | minutes | tokens | note |
|---|---|---|---|---|---|---|
| Opus 5 baseline | 22/22 | 20/20 | 17/17 | - | - | `bench/baselines/spreadsheet-opus.html` |
| muse-spark alone, cline, thinking medium | 22/22 | 14/20 | 5/17 | 21.1 | 29.1M (13.9M cached) | 187 tool calls: it built the page as forty small .js files and concatenated them. The suite is green and the page is a bare grid with dark text on a dark ground; parser, errors, fill, find, csv, chart, validation, persist, keys empty. First attempt at default thinking: "The operation timed out." at 11 min (F17) |
| the same + gate + 1 repair turn | 22/22 | 20/20 | **15/17** | +8.2 | +6.6M | one turn with the 18 defects listed took it from 5 to 15 clicks; undo and the arrow keys stay wrong (ArrowDown moves three cells: the page handles keydown, keyup and the bubbled event). Turns 2 and 3: `Daily free limit reached ... Try again in 23h 14m` |
| deepseek-v4.1-flash alone, Token Harbor | 1/22 | 0/20 | 2/17 | 55 | 3.3M (3.2M input) | 2,017 lines by 36 edits, the 60-step cap (F16), a SyntaxError at line 785 and no envelope; $1.5 at list for a red page. The repair, resumed with the failure reason first in its list, got `free_tier_limit_reached`: Token Harbor's weekly allowance was gone (F19) |
| swarm size 5, glm-5.3-flash in every seat, cline, thinking medium (`162540a7`) | 1/22 | - | - | 49 | 9.0M | the prototype's first try reasoned past cline's 12-minute stream cut (F20); relaunched at medium thinking it wrote 613 lines with 5 block markers, then the 40-minute call budget killed it; 4 of 5 owners then got "empty response" or the glm daily cap; the finisher too. Nothing accepted, and sizes 10, 20, 2, 1 died in ten seconds each on the cap |

What changed against the kanban brief: the soloist is no longer green on the tests-and-page pair
without help; the repair loop is what makes the page usable (5 -> 15 of 17 in one turn), and the
providers' caps and cut-offs, not the models' reasoning, are what stopped every run short of
17/17. By 15:23 all four free gateways were out (Zen dead, cline muse and glm capped, Token
Harbor's week spent); the ladder itself is not measured yet. What the afternoon did settle: a
2,000-line brief is past what any free model here writes in one stream (cline cuts at ~12 min,
the nim loop at its step cap), so the split into blocks is the swarm's job on it, and the
prototype turn (skeleton plus 20 markers) has to be short-thinking or the split has to start from
a skeleton the harness writes itself.
