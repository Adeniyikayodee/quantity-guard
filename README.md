# quantity-guard

Physical quantities carry their unit, vertical datum, timezone, and record quality across
an AI agent's tool boundary as structured metadata the runtime enforces, rather than as
prose the model is trusted to track.

Across 1,901 benchmark runs on six models, every model tested passed a discharge published
in cubic feet per second into a parameter declared in cubic metres per second without
converting it, on every unguarded run. The answer is 35.3 times too large and nothing in
it looks wrong.

```bash
pip install quantity-guard
```

## What it prevents

Dimensional analysis alone does not reach the worst cases. A stage of 12.4 ft above a local
gage datum and a flood stage of 31.0 ft above NAVD88 are both lengths, so a units library
will subtract one from the other and return 18.6 ft, which is not a freeboard. The values
are dimensionally compatible and semantically incompatible.

Four checks run at the boundary where tools are called:

| check | catches |
|---|---|
| Dimensionality | a length where a volumetric flow is required, converting where a conversion exists |
| Reference frame | vertical datums, coordinate systems, and timezones, none visible to dimensional analysis |
| Carry-over | a magnitude reused from an earlier tool with its unit dropped or wrongly relabelled |
| Provenance | a figure in an answer, or entering a tool, that traces to nothing retrieved |

## Guarding a server you did not write

`quantity-guard-mcp` wraps an existing MCP server. It reads the server's tool list, merges
in declarations from an annotation file, re-advertises the tools with units in the schema,
and validates calls in flight.

```bash
quantity-guard-mcp --annotations water.toml -- python -m my_server
```

```toml
[tools.read_discharge]
returns = { unit = "cfs" }

[tools.runoff_depth.params]
discharge = { unit = "m**3/s" }
area = { unit = "km**2" }
```

Arguments are converted into the unit the upstream server already expects, so the server
needs no change: a model sending `1250 cfs` causes the tool to receive `35.4`. Bare numeric
results come back labelled, which is what lets carry-over detection work across a server
the library knows nothing about. Only annotated tools are guarded; the rest pass through.

`demo/usgs_server.py` is an ordinary server that states units in prose and answers in bare
numbers. Behind the proxy:

```
schema   discharge x-unit = m**3/s
ok       {"value": 1250.0, "unit": "cfs"}
ERROR    received the bare number 1250, which this tool reads as 1250 m3/s, but
         read_discharge.return returned 1250 cfs and no conversion was applied
ok       {"value": 0.1054, "unit": "mm / d"}
```

## Declaring tools directly

```python
from quantity_guard import quantity_tool

@quantity_tool(
    params={"discharge": {"unit": "m**3/s"}, "area": {"unit": "km**2"}},
    returns={"unit": "mm/day"},
)
def runoff_depth(discharge, area):
    """Depth-equivalent runoff over the contributing area."""
    return discharge / area
```

The body receives `Q` values, so arithmetic carries units through. Callers may pass a bare
number in the declared unit, a string such as `"1250 cfs"`, or
`{"value": 1250, "unit": "cfs"}`; all normalise before the body runs. A dimensionally wrong
argument is refused rather than coerced.

`json_schema()` emits an MCP tool definition extended with `x-unit`, `x-datum`, `x-crs`, and
`x-tz`. `toolbox([...]).schemas("openai" | "anthropic")` emits the same declarations in those
providers' formats, with dispatch and result wrapping.

### Declaration fields

| field | effect |
|---|---|
| `unit` | required dimensionality; compatible input is converted, incompatible refused |
| `datum` | vertical reference; values on another datum are refused, never silently shifted |
| `tz` | required timezone; a naive timestamp is refused |
| `quality` | weakest acceptable record, so a tool can decline provisional data |
| `crs` | coordinate system, checked for equality |
| `sourced` | value must trace to a tool output, a combination of them, or the question |
| `require_explicit_unit` | a bare number is refused |

Quality flags propagate through arithmetic, taking the weakest input, and USGS single letter
codes are accepted directly. A quantity holds one value or a series, and declarations behave
identically for both (`pip install quantity-guard[arrays]`).

## Vertical datums

Datums are named reference frames. Two quantities on different datums cannot be differenced
or compared, and conversion requires an offset registered explicitly, because the true offset
varies with location.

```python
register_station("07374000", Q(1.5, "ft", datum="NAVD88"))

flood_stage - stage                                # DatumMismatch
flood_stage - stage.to_datum("NAVD88")             # Q(17.1 ft)
Q(31.0, "ft", datum="NAVD88").to_datum("NGVD29")   # DatumConversionUnavailable
```

Differencing two elevations on a shared datum yields a delta carrying no datum, which makes
freeboard arithmetic well defined while leaving the sum of two absolute elevations refused.

## Carry-over between tools

A bare number entering a tool is read in that tool's declared unit, which is the contract the
schema states. The contract breaks when a model reuses a magnitude from an earlier tool
without converting it. Within a session the ledger makes this detectable: if the value a tool
ends up holding equals one an earlier tool returned while the two are on different footings,
no conversion was applied.

```python
with session():
    read_discharge("07374000")                  # returns 1250 cfs
    runoff_depth(discharge=1250, area=29000)
# UnconvertedCarryOver: received the bare number 1250, which this tool reads as
# 1250 m3/s, but read_discharge.return returned 1250 cfs and no conversion was applied
```

The check is keyed on the value the tool will act on rather than on how it was written, so it
covers a dropped unit, a wrongly asserted one (`{"value": 1250, "unit": "m**3/s"}` over a
reading in cfs), and a dropped datum, which gives a wrong answer by an offset rather than a
ratio and is the harder of the three to notice.

## Provenance

Guarded tools record every quantity crossing their boundary, and two checks read that ledger.

`audit_answer` classifies each numeric literal in a final answer. `sourced` matched a recorded
output, `derived` is a sum or difference of recorded outputs, `quoted` was repeated back from
the question, `unsourced` matched nothing, and `unit_mislabelled` matched a magnitude but
contradicted its unit. Only the last two make `audit.ok` false.

```python
with session(context=question) as ledger:
    ...
    audit = ledger.audit_answer(answer, context=question)
```

`sourced=True` on a parameter closes the gap the answer audit cannot: a fabricated *input*
produces a computed *output* the audit then reports as sourced, because a tool really did
return it.

```python
runoff_depth("1250 cfs", 2915830.0)
# UnsourcedInput: received 2.91583e+06 km2, which no tool returned and the question
# did not supply
```

`manifest()` returns the full ledger, enough to re-run a session and check its numbers.

## Reading real data

`quantity_guard.packs.usgs` retrieves from USGS Water Services and keeps what the service
publishes: a unit code per variable, a qualifier marking the record provisional or approved,
an explicit UTC offset per timestamp, and a site record giving the gage datum.

```python
site, values = usgs.reading("07374000")
values["00060"].value      # Q(234000 ft3/s (provisional))
values["00065"].value      # Q(7.73 ft (GAGE:07374000, provisional))
```

Reading the site record registers the station datum, so differencing a gage height against an
absolute elevation is refused. Network access goes through a replaceable `fetch`; tests run
against recorded responses, and `pytest -m live` checks them against the service.

## Adopting incrementally

`enforcement="warn"` validates without rejecting: the call proceeds on the raw value and the
violation is recorded, so the cost of enforcement can be measured before it is paid.

```python
print(ledger.enforcement_report())
# 2 of 3 tool calls would have been blocked:
#   2x dimensionality_error
#       runoff.discharge: expected a quantity in m**3/s ([length]^3 [time]^-1),
#       received 12.4 ft ([length])
```

Every violation carries a `repair()` string stating what was wrong and what to send instead,
and `invoke()` returns it as a tool error rather than raising, so a rejected call stays in the
conversation where the model can correct it.

## Results

`bench/` is a reproducible evaluation. Four conditions hold the tool bodies constant and vary
only the schema shown to the model and whether validation is enforced: `baseline` (plain
schema, bare numeric results), `schema_only` (physical metadata, no enforcement), `guarded`,
and `guarded_repair`. Eight replicates per cell, 1,901 usable runs.

```bash
python -m bench --suite core --model anthropic/claude-opus-5 --replicates 8
```

### Unit carry-over

Runs in which the model skipped the cfs to m3/s conversion, giving an answer 35.3 times too
large.

| model | baseline | guarded |
|---|---|---|
| Claude Haiku 4.5 | 8/8 | 0/8 |
| Claude Sonnet 4.6 | 8/8 | 0/8 |
| Claude Opus 5 | 8/8 | 0/8 |
| DeepSeek V3.2 | 8/8 | 0/8 |
| Qwen3 235B A22B | 8/8 | 0/8 |
| Llama 3.3 70B | 3/8 | 0/8 |

Capability does not protect against this. The frontier model fails as reliably as the
smallest, because the mistake is not one of reasoning: the number arrives with no unit
attached, so there is nothing to reason about. Llama 3.3 is lower only because it fails the
harness protocol often enough not to reach the computing tool, and when it does reach it, it
asserts the wrong unit outright rather than omitting it.

Declaring the unit in the schema removes most but not all of the error and does not order by
capability, ranging from 1 to 6 of 8 across models. Enforcement removes it. Task accuracy over
the core suite moves from 75-83% at baseline to 94-100% enforced, on the five models that
handle the protocol reliably.

### Vertical datum

Silent errors on a levee freeboard task comparing a forecast water surface on NAVD88 against a
crest surveyed on NGVD29, with no converter tool and no signposting.

| model | baseline | guarded |
|---|---|---|
| Qwen3 235B A22B | 8/8 | 0/8 |
| Claude Haiku 4.5 | 3/8 | 0/8 |
| Llama 3.3 70B | 2/8 | 0/8 |
| Claude Sonnet 4.6, Claude Opus 5, DeepSeek V3.2 | 0/8 | 0/8 |

Three of six models report 4.5 ft of freeboard where 4.06 ft is correct, overstating the
margin by 11% in the direction that matters. The other three notice the datum difference
unprompted and fetch the offset.

### Fabricated inputs

Runs in which a model passed a drainage area it had not retrieved, on a task where the area
tool fails for a station whose area is widely memorised.

| model | fired |
|---|---|
| Llama 3.3 70B | 12/16 |
| DeepSeek V3.2 | 1/16 |
| Claude Haiku 4.5, Sonnet 4.6, Opus 5, Qwen3 235B | 0/16 each |

Llama 3.3 supplies the figure from memory in three quarters of guarded runs, and with the
repair loop enabled every one of those runs then reported the value as unavailable. Claude
models never attempted it across 48 runs and three separate task designs, so this check is
load-bearing for some model families and inert for others.

### Scope

A power systems suite (`--suite grid`) carrying the same carry-over hazard into MW, kV, and MVA
did not reproduce it: every model converted correctly on all 192 runs. MW to W and kV to V are
SI prefix conversions, which models perform reliably, whereas cfs to m3/s is a factor of 0.0283
with no prefix relationship, which they do not. The hazard is therefore units with no prefix
relationship to the declared one, covering customary and legacy systems such as US hydrology,
oil and gas, aviation, and building services, rather than units in general.

### The audit's own error rate

Measured over 583 correct answers, the answer audit initially flagged 34% of them, rising to
98% on one task. Every cause was a legitimate number: values derived arithmetically, values
quoted back from the question, a derived value written without a unit, a figure rounded from
17.1 to 17, and a sign carried in the prose rather than the digits. Re-running over 288 fresh
transcripts after those fixes leaves 0% unexplained, while the audit continues to catch genuine
unit misstatements in 12% of runs whose final answer was graded correct.

Derivation follows sums and differences of like dimensions, one step deep. Allowing products
and quotients as well was measured to accept 53% of randomly chosen numbers on a six-output
ledger. With the restriction a random number is accepted 2.6% of the time on a three-output
ledger and 8.0% on a six-output one.

The audit answers provenance, not correctness. A freeboard of 18.6 ft computed as 31.0 minus
12.4 is `derived`, because it came from two recorded outputs; that it used the wrong operation
is a physics error, caught by the datum check at the boundary.

## Limitations

Coordinate reference systems are carried as a consistency tag and checked for equality, never
converted. A scalar has no coordinates to reproject, so reprojection belongs to a geometry type
this library does not define.

The timezone check enforces that an offset is present, not that it is correct. A model pairing
a UTC clock reading with a local offset passes.

Retrieval covers instantaneous values and site records from USGS Water Services. Framework
adapters cover the OpenAI and Anthropic tool formats, not higher-level agent frameworks.

Results come from four task suites in two domains at eight replicates per cell, which separates
the large effects reported above but not differences below roughly ten percentage points.

## Licence

MIT
