# How to author a swarm spec and its solo baseline

You are writing ONE spec for a 20-agent swarm, plus the single-agent baseline it will be
compared against. Everything you produce must be deterministic and checkable without a model.

## Read these first

- `prompts/04-pelican-beach.json` and `prompts/05-galaxy-six.json` are the two working models.
  Copy their shape exactly: same keys, same style of contract, same style of tests.
- `bench/baselines/pelican-opus.svg` and `bench/baselines/galaxy-opus.html` are the matching
  baselines. Yours must be of that standard: a real, finished artifact, not a sketch.

## What you deliver, and nothing else

1. `prompts/<NN>-<slug>.json` - the spec. Keys, in this order:
   - `prompt`: one paragraph naming the subject and what must be visible or true.
   - `definition_of_done`: numbered, testable sentences. Every one must be checked by a test.
   - `contract`: the exact output shape - root element, required ids, required structure, what
     is forbidden (no network, no external assets, no libraries).
   - `context`: `{"language": "...", "framework": "none", "platform": "browser"}`.
   - `agents`: EXACTLY 20 unique lowercase names, see the roster rules below.
   - `output_file`: one file name, `.svg` or `.html`.
   - `tests`: the complete text of a stdlib `unittest` module as ONE JSON string.
   - `budget`: `{"tokens": 30000000}`.
2. `bench/baselines/<slug>-opus.<ext>` - your own solo implementation of that same spec.

Do not touch any other file. Do not run a swarm. Do not commit anything.

## Roster rules (this is what makes the swarm work)

The harness cuts the artifact into named blocks and gives each agent exactly one, so the names
ARE the division of labour:

- 15 to 17 names must map to a DISJOINT region or concern of the file: a visible area, a
  structural element, one function of the model. Two agents must never have a reason to edit
  the same lines. Name them after the thing they own (`hull`, `mast`, `waterline`), never after
  a generic activity (`builder`, `helper`).
- 3 to 5 names are reviewers by nature and own no block: e.g. `measurer`, `skeptic`, `referee`.
- Cross-cutting concerns (palette, typography, shading) count as reviewers unless they own a
  real block such as a `<defs>` section.

## Test rules

- stdlib only: `unittest`, `xml.etree.ElementTree`, `re`, `json`, `subprocess`. No network, no
  third-party imports, no rendering.
- 10 to 14 tests, each one traceable to a line of `definition_of_done`.
- Structural and numeric, never aesthetic: ids exist, counts, ratios, ordering, geometry
  relations (this is above that, these two are equal within 2%, this is inside that).
- For an HTML artifact, keep the maths in a `<script id="...-model">` block holding pure
  functions, and test it by running node on extracted source (as `05-galaxy-six.json` does).
  Skip the test with `unittest.SkipTest` if node is missing, never fail.
- The suite must FAIL on an empty file and on a placeholder, and PASS on your baseline.
- No test may depend on your own implementation's private details: another author's correct
  artifact must pass too.

## Before you report back

1. Write the tests BEFORE the baseline. Then build the baseline until it passes.
2. Run them for real, in a temp directory, with the artifact named exactly `output_file`:
   `.venv/Scripts/python.exe -I -m unittest discover -s <tmp>`
3. Check the suite fails on an empty file of the same name.
4. Report: the spec path, the baseline path, the test count, the pass line, and the roster
   split (how many block owners, how many reviewers).
