# quantity-guard

Physical quantities carry their unit, vertical datum, timezone, and record quality across
an AI agent's tool boundary as structured metadata the runtime enforces, rather than as
prose the model is trusted to track.

Across 3,639 benchmark runs on eleven models from seven families, every model that
reached the computing tool passed a discharge
published in cubic feet per second into a parameter declared in cubic metres per second
without converting it, on every unguarded run. The answer is 35.3 times too large and
nothing in it looks wrong.

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

Nothing in the library is specific to one country. Units come from `pint`, timezones are
IANA names, and datums are registered by name, so the checks apply wherever the data comes
from. Twenty-two national and tidal datums are pre-registered, including ODN, NAP,
EVRF2019, DHHN2016, AHD, CGVD2013, NZVD2016, and LAT, and any other can be added with
`datums.register`. An offset between two of them is never assumed, because it varies with
location everywhere, not only in North America.

Two retrieval packs are included, one American and one British, mainly to show that the
hazard has the same shape in both.

```python
from quantity_guard.packs import usgs, ea

site, values = usgs.reading("07374000")
values["00060"].value          # Q(234000 ft3/s (provisional))
values["00065"].value          # Q(7.73 ft (GAGE:07374000, provisional))

station, levels = ea.reading("E21136")
levels[0].value                # Q(0.117 m (GAUGE:E21136))
levels[0].value.to_datum("ODN")  # Q(6.417 m (ODN))
```

The vocabularies differ and the problem does not. USGS publishes gage height against a
station datum recorded in the site metadata; the Environment Agency publishes levels as
`mASD`, metres above the station's own zero, or `mAOD`, metres above Ordnance Datum Newlyn,
with the offset between them in the station record. In both cases reading the station record
registers the local datum, so differencing a stage against an absolute elevation is refused
rather than quietly wrong.

Both services publish more than a number, and both are usually read as though they did not:
a unit per variable, a quality qualifier, an explicit timezone, and a datum. Network access
goes through a replaceable `fetch`; tests run against recorded responses, and `pytest -m live`
checks them against the services.

Quality codes are mapped per agency. USGS single letters and Environment Agency words are
both understood, and `QUALITY_ALIASES` takes entries for any other publisher.

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
and `guarded_repair`. Eight replicates per cell, 3,639 usable runs across eleven models.

```bash
python -m bench --suite core --model anthropic/claude-opus-5 --replicates 8
```

### Unit carry-over

Runs at baseline in which the model passed a discharge published in cfs into a parameter
declared in m3/s without converting it. Counted on what reached the tool rather than on the
final answer, because a model that carries the error forward through further arithmetic
still made it.

| model | passed on unconverted |
|---|---|
| Claude Opus 5 | 8/8 |
| Claude Sonnet 4.6 | 8/8 |
| Claude Haiku 4.5 | 8/8 |
| DeepSeek V3.2 | 8/8 |
| Qwen3 235B A22B | 8/8 |
| Kimi K2 | 8/8 |
| gpt-oss 120B | 8/8 |
| Mistral Large | 8/8 |
| Gemma 3 27B | 8/8 |
| Llama 3.3 70B | 17/17 |

Every model that reached the computing tool made the error on every run. Neither capability
nor family predicts it: the number arrives with no unit attached, so there is nothing to
reason about, and a stronger model reasons better about the wrong object. Llama 4 Maverick
is absent because it answers without calling tools at all, at 0.4 calls per run against 2.3
to 2.9 for the rest, and is caught by the provenance audit instead.

Under enforcement the error reaches the tool on no run of any model. Declaring the unit in
the schema alone removes most but not all of it and does not order by capability. Task
accuracy over the core suite moves from 75-83% at baseline to 94-100% enforced, on the
models that follow the tool protocol reliably.

### Vertical datum

Silent errors on a levee freeboard task comparing a forecast water surface on NAVD88 against
a crest surveyed on NGVD29, with no converter tool and no signposting.

| model | baseline | guarded |
|---|---|---|
| Qwen3 235B A22B | 8/8 | 0/8 |
| Gemma 3 27B | 8/8 | 0/8 |
| gpt-oss 120B | 8/8 | 0/8 |
| Claude Haiku 4.5 | 3/8 | 0/8 |
| Llama 3.3 70B | 2/8 | 0/8 |
| Kimi K2 | 1/8 | 0/8 |
| Mistral Large | 1/8 | 0/8 |
| Claude Opus 5, Sonnet 4.6, DeepSeek V3.2, Llama 4 Maverick | 0/8 | 0/8 |

Seven of eleven models report 4.5 ft of freeboard where 4.06 ft is correct, three of them on
every run, overstating the margin by 11% in the direction that matters. The rest notice the
datum difference unprompted and fetch the offset. Whether a model handles this does not
track its size or its score on the other tasks.

### Fabricated inputs

Runs in which a model passed a drainage area it had not retrieved, on a task where the area
tool fails for a station whose area is widely memorised.

| model | fired |
|---|---|
| Llama 3.3 70B | 12/16 |
| Kimi K2 | 4/16 |
| Mistral Large | 3/16 |
| DeepSeek V3.2 | 1/16 |
| Claude Opus 5, Sonnet 4.6, Haiku 4.5, Qwen3 235B, Gemma 3 27B, gpt-oss 120B, Llama 4 Maverick | 0/16 each |

Four of eleven models supply the figure from memory, Llama 3.3 in three quarters of guarded
runs. With the repair loop enabled, every one of those runs then reported the value as
unavailable. No Claude model attempted it across 48 runs and three separate task designs.
The check is therefore load-bearing for some model families and inert for others, which is
not visible from any single-family evaluation.

### Which unit pairs

`--suite uk` repeats the task in British water units, where a licensed abstraction is
published in megalitres per day and river flow in cubic metres per second. Same harness,
same structure, different unit.

| model | cfs to m3/s | Ml/d to m3/s |
|---|---|---|
| Claude Opus 5 | 8/8 | 0/8 |
| Claude Sonnet 4.6 | 8/8 | 0/8 |
| Kimi K2 | 8/8 | 0/8 |
| gpt-oss 120B | 8/8 | 0/6 |
| Mistral Large | 8/8 | 0/8 |
| DeepSeek V3.2 | 8/8 | 1/11 |
| Claude Haiku 4.5 | 8/8 | 3/8 |
| Qwen3 235B A22B | 8/8 | 7/8 |

Seven of eight models convert Ml/d reliably and none of them converts cfs. The conversion
factor does not explain it: cfs to m3/s is 0.0283 and Ml/d to m3/s is 0.0116, both awkward
and neither a prefix relationship. What differs is that `Ml/d` states its own composition
and `cfs` does not. A model reading `Ml/d` can see megalitres per day; a model reading `cfs`
has to already know the expansion.

So the hazard is narrower than non-SI units. It is opaque abbreviations that hide what they
stand for, which is where customary systems concentrate them: cfs, gpm, MGD, cusec, acre-ft,
psi, scf. The prediction is that a service publishing `ft**3/s` in full would be safer than
one publishing `cfs`, which this suite does not test.

Qwen3 235B is the exception and fails both, so opacity is not the whole account. Gemma 3 27B
was rate limited on 52 of 96 British runs and is excluded from that column.

Two secondary results came from the same suite. A level published as mASD against a warning
threshold in mAOD is the British form of the datum hazard, and the guard rejects the attempt
to difference them. Separately, Haiku restated a correct tool result of 1350 Ml as 1.35 Ml
in prose, which the audit flagged as both unsourced and mislabelled: the arithmetic was right
and the reporting was not.

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

Retrieval covers instantaneous values and site records from USGS Water Services, and
stations and latest measures from the Environment Agency. Other services need a pack of
their own, though the core needs nothing added to work with them. Framework adapters cover
the OpenAI and Anthropic tool formats, not higher-level agent frameworks.

The opacity reading rests on one unit pair in one domain, with one model contradicting it.
It predicts an ordering over unit names that has not been tested directly.

Results come from four task suites in two domains at eight replicates per cell, which
separates the large effects reported above but not differences below roughly ten percentage
points. Llama 4 Maverick reaches 0.4 tool calls per run and Gemma 3 27B and Llama 3.3 70B
fail to emit a parseable answer on a substantial fraction of runs, so their unit results
describe fewer completed tool paths than the denominators suggest.

## Licence

MIT
