# Golden regression harness

Deterministic electrical-correctness gate for the schematic pipeline
(`normalize → validate → render`). One command tells you whether a code change
regressed any known circuit.

## Run

```
python tests/golden/run_golden.py                # gate: exit 1 on any regression
python tests/golden/run_golden.py --determinism  # 0.0 probe: output must be reproducible
python tests/golden/run_golden.py --update       # (re)seed baselines from current output
python tests/golden/run_golden.py --only ne555   # one fixture (substring match)
python tests/golden/run_golden.py --no-tier2     # skip the kicad-cli netlist hash
```

## How it works

For each `fixtures/*.json` (pure IR data) the runner builds a deterministic
**snapshot**: validation issue codes/counts, render stats, R2 body-crossings,
R16 cross-block wires, and a two-tier connectivity hash:

- **Tier 1** — post-normalize IR `net → sorted(ref.pin)` adjacency (pure-python,
  always on).
- **Tier 2** — `kicad-cli sch export netlist` of the rendered file (exactly what
  KiCad's connectivity engine sees). Catches render-layer bugs Tier 1 can't
  (dropped wire, label leak, missing junction). Runs only when `kicad-cli` is
  resolvable (via `layout_config.json:kicad_cli_command`, then PATH).

The **exit-code gate is baseline drift**: the snapshot must equal the committed
`baselines/<name>.baseline.json`. This makes the net seedable + green today even
while a circuit carries a known defect.

`golden_config.json:universal_invariants` (0 errors, 0 R2, 0 R16, no forbidden
codes) is the **quality bar**. Under `gate.mode="regression"` (default) a
violation already in the baseline is reported as a `KNOWN` issue and printed
**loudly when blessed** at `--update` — never silently. Set `gate.mode="strict"`
to also fail the gate on any invariant violation, once the engine emits zero.

## Add a fixture

Drop a JSON in `fixtures/` — pure data, no code:

```json
{ "name": "my_circuit", "ir": { ...TopologyIR.to_dict()... } }
```

A fixture may add an optional `"expected"` block to override the universal
invariants (used by intentional-defect fixtures, e.g. `"require_codes":
["CROSS_BLOCK_NET_COLLISION"]`). Then `--update` to seed its baseline and review
any `QUALITY (blessed)` lines before committing.

## No hardcoding

Every threshold, selector param, canonicalization rule, strip pattern, and the
`kicad-cli` location lives in `golden_config.json` / the project's
`layout_config.json`. `run_golden.py` contains no magic numbers and no
per-circuit logic — fixtures are discovered by glob.

## Known issues surfaced at seed time (2026-06-11)

The harness flagged these pre-existing defects in the seed circuits (lifted from
`rebuild_all.py`); they are baselined as `KNOWN`, not fixed here:

- **ne555** — 1 R2 body-pierce: the +5V wire overshoots U1 pin 4 (`~RST`) tip by
  3.81 mm into the IC body to reach a riser placed inside the footprint.
- **lm386** — `PIN_IN_MULTIPLE_NETS`: `C3.2` is on both `+9V` and `SPK` (the
  speaker output is shorted to +9V) in the source IR.
