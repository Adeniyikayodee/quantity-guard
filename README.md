# quantity-guard

Physical quantities carry their unit, vertical datum, timezone, and record quality across
an AI agent's tool boundary as structured metadata that the runtime enforces, rather than
as prose the model is trusted to track.

In a benchmark of 4,288 attempted runs across eleven models from eight families, every
model that reached the computing tool at baseline sent a discharge published in cubic feet
per second into a parameter declared in cubic metres per second without converting it, on
nearly every run. The resulting answer is 35.3 times too large, and because the magnitude
is well formed and every calculation downstream of it is performed correctly, nothing in
the output indicates that anything has gone wrong.

```bash
pip install quantity-guard
```

## What it prevents

Dimensional analysis alone does not reach the worst cases, because a stage of 12.4 ft above
a local gage datum and a flood stage of 31.0 ft above NAVD88 are both lengths, so a units
library will subtract one from the other and return 18.6 ft, which is not a freeboard. The
two values are dimensionally compatible and semantically incompatible, and only the second
of those properties is visible to a units library.

Four checks run at the boundary where tools are called:

| check | catches |
|---|---|
| Dimensionality | a length where a volumetric flow is required, converting where a conversion exists |
| Reference frame | vertical datums, coordinate systems, and timezones, none visible to dimensional analysis |
| Carry-over | a magnitude reused from an earlier tool with its unit dropped or wrongly relabelled |
| Provenance | a figure in an answer, or entering a tool, that traces to nothing retrieved |

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

The body receives `Q` values, so arithmetic carries units through it. Callers may pass a
bare number in the declared unit, a string such as `"1250 cfs"`, or
`{"value": 1250, "unit": "cfs"}`, all of which normalise before the body runs, while a
dimensionally wrong argument is refused rather than coerced.

Service spellings are accepted wherever a unit is read, so that a model is not penalised
for stating a unit in the form its source published. Since `ft3/s` is what the USGS emits
as a unit code and what models write in prose, `Q.parse("1250 ft3/s")`, `Q(1250, "m3/s")`,
and `"150000 acre-ft"` all parse.

`json_schema()` emits an MCP tool definition extended with `x-unit`, `x-datum`, `x-crs`,
and `x-tz`, and `toolbox([...]).schemas("openai" | "anthropic")` emits the same
declarations in those providers' formats, with dispatch and result wrapping. OpenAI's
strict function calling validates the schema itself and rejects any keyword it does not
recognise, so `schemas("openai", strict=True)` moves those extensions into the parameter
description and emits the restricted dialect that mode accepts.

### Declaration fields

| field | effect |
|---|---|
| `unit` | required dimensionality; compatible input is converted, incompatible refused |
| `datum` | vertical reference; values on another datum are refused, never silently shifted |
| `tz` | required timezone; a naive timestamp is refused |
| `quality` | weakest acceptable record; weaker and unstated record are both refused |
| `crs` | coordinate system, checked for equality |
| `sourced` | value must trace to a tool output, a combination of them, or the question |
| `require_explicit_unit` | a bare magnitude is refused, in every input shape |

## Vertical datums

Datums are named reference frames, so two quantities on different datums cannot be
differenced or compared, and conversion between them requires an offset that has been
registered explicitly, because the true offset varies with location.

```python
register_station("07374000", Q(1.5, "ft", datum="NAVD88"))

flood_stage - stage                                # DatumMismatch
flood_stage - stage.to_datum("NAVD88")             # Q(17.1 ft)
Q(31.0, "ft", datum="NAVD88").to_datum("NGVD29")   # DatumConversionUnavailable
```

Differencing two elevations on a shared datum yields a delta that carries no datum, which
is what makes freeboard arithmetic well defined. The remaining combinations are refused,
because each would produce a value that is neither an elevation nor a delta while still
presenting as one:

| left | right | result |
|---|---|---|
| elevation on X | elevation on X | delta, no datum |
| elevation on X | elevation on Y | `DatumMismatch` |
| elevation on X | delta | elevation on X |
| delta | elevation on Y | `DatumMismatch` |

Scaling is refused for any value carrying a datum, by a plain number as much as by another
quantity, since twice an elevation is not an elevation. Permitting it would return a
datum-free delta, which is the one shape that passes every downstream check, so
`elevation * 1` would otherwise launder an absolute value past the datum guard entirely.

Twenty-two national and tidal datums are pre-registered, including ODN, NAP, EVRF2019,
DHHN2016, AHD, CGVD2013, NZVD2016, and LAT, and any other can be added with
`datums.register`. An offset between two of them is never assumed, because it varies with
location everywhere, not only in North America.

## Carry-over between tools

A bare number entering a tool is read in that tool's declared unit, which is the contract
the schema states, and that contract breaks when a model reuses a magnitude from an earlier
tool without converting it. Within a session the ledger makes this detectable, because if
the value a tool ends up holding equals one an earlier tool returned while the two are on
different footings, then no conversion was applied.

```python
with session():
    read_discharge("07374000")                  # returns 1250 cfs
    runoff_depth(discharge=1250, area=29000)
# UnconvertedCarryOver: received the bare number 1250, which this tool reads as
# 1250 m3/s, but read_discharge.return returned 1250 cfs and no conversion was applied
```

The check is keyed on the value the tool will act on rather than on how it was written, so
it covers a dropped unit, a wrongly asserted one such as `{"value": 1250, "unit": "m**3/s"}`
over a reading in cfs, and a dropped datum, the last of which gives a wrong answer by an
offset rather than by a ratio and is therefore the hardest of the three to notice.

Detection compares magnitudes within a tight tolerance, so it identifies a magnitude that
was passed through unchanged, and it does not identify a conversion that was attempted with
the wrong factor, which is a different error that this check does not claim to cover.

## Record quality

Quality flags propagate through arithmetic, taking the weakest input at each step.
Publisher codes are mapped per agency, covering USGS review-status letters (`A`, `R`, `P`,
`e`), USGS condition codes (`Ice`, `Bkw`, `Eqp`, `Fld`, `Rat`, `ZFl`, and others), and
Environment Agency words (`Good`, `Unchecked`, `Suspect`, and others), while
`QUALITY_ALIASES` takes entries for any other publisher.

Condition codes are graded rather than discarded, because an ice-affected or
backwater-affected discharge can be wrong by a large margin while remaining entirely
plausible. A reading marked `["A", "Ice"]` therefore grades as `unverified`, on the
grounds that approved record of an ice-affected measurement is not approved-quality data.

A declared floor also refuses record whose grade is unstated, since absence of a qualifier
is not evidence of approval, and accepting it would make the requirement satisfiable by
discarding the flag.

## Provenance

Guarded tools record every quantity crossing their boundary, and two checks read that
ledger. The first, `audit_answer`, classifies each numeric literal in a final answer:

| status | meaning | fails `ok` |
|---|---|---|
| `sourced` | matched a recorded output | |
| `derived` | a sum or difference of recorded outputs, one step deep | |
| `quoted` | repeated back from the question | |
| `unsourced` | matched nothing | yes |
| `unit_mislabelled` | matched a magnitude but contradicted its unit | yes |
| `sign_inverted` | matched a magnitude with the sign reversed, unaccounted for | yes |

```python
with session(context=question) as ledger:
    ...
    audit = ledger.audit_answer(answer, context=question)
```

A magnitude is matched with its sign ignored, because a tool returning a datum offset of
-0.44 ft is correctly restated as "subtract 0.44 ft", where the sign has moved into the
prose rather than being lost. Which way the match was made is a separate fact, however, so
a flip that no surrounding wording accounts for is reported as `sign_inverted`. On a
freeboard that distinction separates margin from overtopping, since a tool returning
-4.06 ft reported as "4.06 ft of freeboard remaining" describes the opposite condition.

The second check, `sourced=True` on a parameter, closes a gap the answer audit cannot
reach, because a fabricated input produces a computed output that the audit will then
report as sourced, a tool having genuinely returned it.

```python
runoff_depth("1250 cfs", 2915830.0)
# UnsourcedInput: received 2.91583e+06 km2, which no tool returned and the question
# did not supply
```

`manifest()` returns the full ledger, which is sufficient to re-run a session and check its
numbers.

## Reading real data

Nothing in the library is specific to one country, since units come from `pint`, timezones
are IANA names, and datums are registered by name, so the checks apply wherever the data
originates. Two retrieval packs are included, one American and one British, mainly to show
that the hazard has the same shape in both.

```python
from quantity_guard.packs import usgs, ea

site, values = usgs.reading("07374000", max_age=timedelta(hours=6))
values["00060"].value          # Q(234000 ft3/s (provisional))
values["00065"].value          # Q(7.73 ft (GAGE:07374000, provisional))

station, levels = ea.reading("E21136")
levels[0].value                # Q(0.117 m (GAUGE:E21136))
levels[0].value.to_datum("ODN")  # Q(6.417 m (ODN))
```

The two agencies use different vocabularies for the same underlying problem, in that USGS
publishes gage height against a station datum recorded in the site metadata, whereas the Environment Agency
publishes levels either as `mASD`, metres above the station's own zero, or as `mAOD`,
metres above Ordnance Datum Newlyn, with the offset between them held in the station
record. In both cases reading the station record registers the local datum, so differencing
a stage against an absolute elevation is refused rather than being quietly wrong.

Three properties of these services are easy to lose in retrieval, and each is handled
explicitly.

### A station datum is registered whether or not it can be converted

Only about one in eight Environment Agency stations publishes a `datumOffset`, and many
USGS site records state an altitude without stating which datum it is measured from. In
both cases the station's own frame is still registered, so a level is labelled with the
frame it was measured from, while `to_datum` raises `DatumConversionUnavailable`. The datum
itself is never assumed, because guessing which datum an altitude is measured from is the
same class of error as guessing an offset between two datums, arriving one step earlier and
being correspondingly harder to observe.

### "Latest" and "current" are not the same thing

Both services return the last value they hold for each parameter independently, so a single
response can mix ages freely, as observed live at USGS 01646500:

```
00060 Streamflow            3010 ft3/s   2026-08-15   <- current
00065 Gage height           3.03 ft      2026-08-15   <- current
00010 Temperature           24.3 degC    2019-10-01   <- seven years old
63680 Turbidity             6.2 FNU      2019-05-27   <- seven years old
```

`Observation.age` and `is_stale()` make the question answerable, and `max_age` drops stale
readings while emitting a warning that names what was dropped. It defaults to `None`, which
is faithful to the endpoint and is rarely what a caller wants.

### A missing value is not a measurement

NWIS publishes its own sentinel for each variable in the `noDataValue` field, which is read
rather than assumed, so every spelling of it is caught. Absent that, a discharge of
-999,999 ft³/s would be dimensionally valid and would satisfy every check in this library.

Network access goes through a replaceable `fetch`, so tests run against recorded responses
while `pytest -m live` checks them against the services. An unmapped unit skips its own
series with a warning rather than failing the whole station read.

## Guarding a server you did not write

`quantity-guard-mcp` wraps an existing MCP server, reading the server's tool list, merging
in declarations from an annotation file, re-advertising the tools with units in the schema,
and validating calls in flight.

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
needs no change, and a model sending `1250 cfs` causes the tool to receive `35.4`. Return
units are advertised in the tool's `outputSchema` and description, which is where a model
would otherwise have no way to learn what an opaque abbreviation stands for. Bare numeric
results come back labelled, which is what allows carry-over detection to work across a
server the library knows nothing about. Only annotated tools are guarded, and the rest pass
through untouched.

`demo/usgs_server.py` is an ordinary server that states its units in prose and answers in
bare numbers. Behind the proxy it behaves as follows:

```
schema   discharge x-unit = m**3/s
ok       {"value": 1250.0, "unit": "cfs"}
ERROR    received the bare number 1250, which this tool reads as 1250 m3/s, but
         read_discharge.return returned 1250 cfs and no conversion was applied
ok       {"value": 0.1054, "unit": "mm / d"}
```

The ledger is scoped to a client session and reset on `initialize`, so quantities from one
conversation cannot trigger a carry-over violation in another, and it is bounded in size so
that a long-running server does not accumulate one without limit.

## Adopting incrementally

Setting `enforcement="warn"` validates without rejecting, so the call proceeds on the raw
value while the violation is recorded, on the argument path and the return path alike,
which allows the cost of enforcement to be measured before it is paid.

```python
print(ledger.enforcement_report())
# 2 of 3 tool calls would have been blocked:
#   2x dimensionality_error
#       runoff.discharge: expected a quantity in m**3/s ([length]^3 [time]^-1),
#       received 12.4 ft ([length])
```

Every violation carries a `repair()` string stating what was wrong and what to send
instead, and `invoke()` returns it as a tool error rather than raising, which keeps a
rejected call inside the conversation where the model can correct it.

## Evaluation

### Method

`bench/` contains a reproducible evaluation in which four conditions hold the tool bodies
constant and vary only the schema shown to the model and whether validation is enforced:

| condition | schema | enforcement |
|---|---|---|
| `baseline` | units in prose, bare numeric results | none |
| `schema_only` | physical metadata declared | none |
| `guarded` | physical metadata declared | on, a rejected call ends the run |
| `guarded_repair` | physical metadata declared | on, the repair string returns to the model |

```bash
python -m bench --suite core --model anthropic/claude-opus-5 --replicates 8
```

The design covers eleven models from eight families and fourteen tasks over four hazards
(unit carry-over, vertical datum, provenance, and timezone), giving 432 cells of model by
task by condition, with eight replicates in most cells, of which 4,288 runs were attempted
and 3,639 were usable. A run is dropped when the provider returns an API or protocol error rather than a
task outcome, which accounts for 15.1% overall and is very unevenly distributed, ranging
from 0% for Sonnet 4.6, Llama 3.3, and Mistral Large to 27.8% for Gemma 3 27B, and dropped
runs are excluded from every denominator reported below.

Two distinct measurements appear in the results and should not be read as interchangeable.
The first, described below as sent to the tool, is read from the recorded call log and
captures which argument the model actually passed, independently of whether a guard then
rejected it. The second, a silent error, is a wrong number stated in the final answer with
no guard violation raised and nothing flagged by the audit, and it captures what a user
would have been shown.

### Unit carry-over

The table below counts runs in which the model sent a discharge published in cfs into a
parameter declared in m³/s without converting it, measured from the call log over runs that
reached the computing tool at all.

| model | baseline |
|---|---|
| Claude Opus 5, Sonnet 4.6, Haiku 4.5 | 8/8 each |
| DeepSeek V3.2, Qwen3 235B A22B, Kimi K2 | 8/8 each |
| gpt-oss 120B, Mistral Large, Gemma 3 27B | 8/8 each |
| Llama 3.3 70B | 14/15 |

Every model that reached the computing tool made the error on nearly every run, and neither
capability nor family orders the result, which is consistent with the mechanism being an
absence rather than a difficulty: the number arrives with no unit attached, so there is
nothing to reason about, and a stronger model reasons better about the wrong object. Llama
4 Maverick is absent from the table because it does not reach the tool, averaging 0.4 tool
calls per run against 2.6 to 4.9 for the rest, and it is caught by the provenance audit
instead.

Under enforcement the model continues to send the unconverted magnitude at similar rates,
so the guard rejects the call rather than changing what the model attempts. Enforcement
should therefore be understood as a boundary check rather than as a correction to the
model's reasoning, which bears on what it can be relied upon to do. Its effect on this task,
pooled over all eleven models at 96 usable runs per condition, is as follows:

| condition | correct | wrong | blocked | silent errors |
|---|---|---|---|---|
| `baseline` | 0 | 96 | 0 | 46 |
| `schema_only` | 33 | 63 | 0 | 46 |
| `guarded` | 72 | 13 | 11 | 3 |
| `guarded_repair` | 75 | 21 | 0 | 2 |

No model answers this task correctly at baseline, and declaring the unit in the schema alone
recovers a third of the runs while leaving the silent-error count unchanged, because the
runs it fixes are not the runs that were failing silently, and enforcement is what moves
silent errors from 46 to 3. That figure does not reach zero, since two runs under
`guarded_repair` still state a wrong number with nothing flagged, so the residual rate is
small rather than nil, and most of the wrong answers remaining under that condition are
signalled rather than silent.

### Vertical datum

The table below counts silent errors on a levee freeboard task comparing a forecast water
surface on NAVD88 against a crest surveyed on NGVD29, with no converter tool available and
no signposting in the prompt.

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

Seven of eleven models report 4.5 ft of freeboard where 4.06 ft is correct, three of them
on every run, overstating the available margin by 11%. The remaining models notice the
datum difference without prompting and fetch the offset, and whether a model handles this
does not track its size or its score on the other tasks.

### Fabricated inputs

The table below counts runs in which a model passed a drainage area it had not retrieved, on
a task where the area tool fails for a station whose area is widely memorised.

| model | fired |
|---|---|
| Llama 3.3 70B | 12/16 |
| Kimi K2 | 4/16 |
| Mistral Large | 3/16 |
| DeepSeek V3.2 | 1/16 |
| Claude Opus 5, Sonnet 4.6, Haiku 4.5, Qwen3 235B, Gemma 3 27B, gpt-oss 120B, Llama 4 Maverick | 0/16 each |

Four of eleven models supply the figure from memory, with Llama 3.3 doing so in three
quarters of guarded runs, and with the repair loop enabled every one of those runs then
reported the value as unavailable. No Claude model attempted it across 48 runs and three
separate task designs, so the check is load-bearing for some model families and inert for
others, which would not be visible from an evaluation covering a single family.

### Task accuracy

Pooled across models and tasks over the water and UK suites:

| condition | correct | blocked |
|---|---|---|
| `baseline` | 611/867 (70.5%) | 0 |
| `schema_only` | 651/863 (75.4%) | 0 |
| `guarded` | 694/858 (80.9%) | 68 |
| `guarded_repair` | 737/859 (85.8%) | 0 |

These pooled figures hide a wide spread between models, in that on the core suite the seven
models
that follow the tool protocol reliably move from a baseline range of 60% to 86% up to 98%
to 100% with enforcement and repair, while the four that do not, being Gemma 3 27B, Llama
3.3 70B, Llama 4 Maverick, and to a lesser extent Mistral Large, start between 22% and 58%
and improve by less, because their failures are protocol failures that this library does
not address.

### Which unit pairs

The `--suite uk` option repeats the task in British water units, where a licensed
abstraction is published in megalitres per day and river flow in cubic metres per second,
using the same harness and the same structure with a different unit. Both columns are
measured from the call log, as above.

| model | cfs into m³/s | Ml/d into m³/s |
|---|---|---|
| Claude Opus 5, Sonnet 4.6, Haiku 4.5 | 8/8 | 0/16 |
| Kimi K2 | 8/8 | 0/15 |
| gpt-oss 120B | 8/8 | 0/14 |
| Mistral Large | 8/8 | 0/16 |
| DeepSeek V3.2 | 8/8 | 1/16 |
| Qwen3 235B A22B | 8/8 | 13/16 |

Seven of eight models convert Ml/d reliably while none of them converts cfs, and the
conversion factor does not explain the difference, since cfs to m³/s is 0.0283 and Ml/d to
m³/s is 0.0116, both awkward and neither a prefix relationship.

One hypothesis is consistent with the data, which is that `Ml/d` states its own composition
while `cfs` does not, so a model reading `Ml/d` can see megalitres per day whereas a model
reading `cfs` must already know the expansion. If that account is correct then the hazard is
narrower than non-SI units in general, being confined to opaque abbreviations that hide what
they stand for, which is where customary systems concentrate them, as in cfs, gpm, MGD,
cusec, acre-ft, psi, and scf. The account predicts that a service publishing `ft**3/s` in
full would be safer than one publishing `cfs`, which this suite does not test, and Qwen3
235B fails both columns, so opacity is not the whole explanation. Gemma 3 27B was rate
limited on most British runs and is excluded from that column.

Two secondary results came from the same suite, the first being that a level published as
mASD against a warning threshold in mAOD is the British form of the datum hazard, which the
guard rejects when an attempt is made to difference them. Separately, Haiku restated a correct tool result of 1350 Ml as 1.35 Ml
in prose, which the audit flagged as both unsourced and mislabelled, the arithmetic having
been right and the reporting not.

### Scope

A power systems suite (`--suite grid`) carrying the same carry-over hazard into MW, kV, and
MVA did not reproduce it. All 192 runs completed, and at baseline every model converted
correctly on all 48 with no silent errors. MW to W and kV to V are SI prefix conversions,
which models perform reliably, whereas cfs to m³/s is a factor of 0.0283 with no prefix
relationship. On this evidence the hazard is units bearing no prefix relationship to the
declared one, covering customary and legacy systems such as US hydrology, oil and gas,
aviation, and building services, rather than units in general.

One cell in that suite runs against the general pattern:

| condition | correct | silent errors |
|---|---|---|
| `baseline` | 48/48 | 0 |
| `schema_only` | 41/48 | 4 |
| `guarded` | 48/48 | 0 |
| `guarded_repair` | 48/48 | 0 |

Declaring the units in the schema without enforcing them made this suite perform worse than
leaving them in prose. All four silent errors come from Haiku on the same task, stating
0.176 A where 175.7 A is correct, a factor of 1000 that constitutes a prefix error
introduced in the presence of the metadata rather than in spite of it. On four cases from
one model and one task this is an observation rather than a result, and it is reported
because it is the only cell in which the `schema_only` condition underperformed `baseline`.

The timezone hazard produces the weakest of the four effects, with silent errors falling
from 22/112 at baseline to 3/104 guarded, on fewer runs than the unit and datum results.

### The audit's own error rate

Measured over 583 correct answers, the answer audit initially flagged 34% of them, rising to
98% on one task. Every cause proved to be a legitimate number, specifically values derived
arithmetically, values quoted back from the question, a derived value written without a
unit, a figure rounded from 17.1 to 17, and a sign carried in the prose rather than in the
digits. Re-running over 288 fresh transcripts after those fixes left 0% unexplained, while
the audit continued to catch genuine unit misstatements in 12% of runs whose final answer
was graded correct.

Those figures predate the `sign_inverted` check and the tokenisation changes listed under
Changes below, which were verified against unit tests rather than re-measured over the
transcript corpus, so the false-positive rate should be treated as re-measurable rather
than as carried forward.

Derivation follows sums and differences of like dimensions, one step deep. Allowing products
and quotients as well was measured to accept 53% of randomly chosen numbers on a six-output
ledger, whereas with the restriction in place a random number is accepted 2.6% of the time
on a three-output ledger and 8.0% on a six-output one. Those rates apply to randomly chosen
numbers and understate the small-integer case, since on a three-output ledger holding 3, 5,
and 11 ft the values 8, 14, and 2 are all reachable as one-step sums or differences.

The audit answers provenance rather than correctness, so a freeboard of 18.6 ft computed as
31.0 minus 12.4 is classified as `derived`, because it came from two recorded outputs, and
the fact that it used the wrong operation is a physics error caught by the datum check at
the boundary instead.

## Limitations

### Design

Results come from four task suites in two domains, with eight replicates per cell,
one run per replicate, and no confidence intervals reported. The design separates the large
effects above, such as 8/8 against 0/8, but does not resolve differences below roughly ten
percentage points. Model identity is confounded with provider infrastructure, because the
15.1% dropped-run rate is uneven across models, so a model dropped more often is measured on
a non-random subset of its runs. Llama 4 Maverick reaches 0.4 tool calls per run, and Gemma
3 27B and Llama 3.3 70B fail to emit a parseable answer on a substantial fraction of runs,
so their results describe fewer completed tool paths than the denominators suggest.

### Reference frames

Coordinate reference systems are carried as a consistency tag and checked
for equality, never converted, because a scalar has no coordinates to reproject and
reprojection therefore belongs to a geometry type this library does not define. The tag
is checked wherever two quantities are combined or compared, so a product of values in
two frames is refused rather than stamped with one of them.

### Time

The timezone check enforces that an offset is present rather than that it is correct,
so a model pairing a UTC clock reading with a local offset will pass. The check also
converts the timestamp into the declared zone before the tool body sees it, so the body
receives a different clock reading from the one the model sent. Freshness is checked only at
retrieval, through `max_age`, and `Q` carries no observation time, so a stale value already
in the ledger is indistinguishable from a current one downstream.

### Carry-over

Detection compares exact magnitudes, so a magnitude rounded during the hand-off
is not matched, and a conversion applied with the wrong factor is not detected at all.

### Audit

A multi-word unit written with spaces, as in "150000 acre feet", tokenises on the
first word only. Derivation false-accepts small integers at a higher rate than the measured
random-number figures imply. Bare integers of eight digits or more, along with any number
introduced by an identifier word, are treated as identifiers and are not audited.

### Retrieval

Coverage extends to instantaneous values and site records from USGS Water
Services, and to stations and latest measures from the Environment Agency. The EA real-time
API publishes no record-quality grade on either measures or readings, so UK quality
enforcement applies to archive data rather than to live data. Levels published as `mBDAT`
are deliberately unmapped, because they measure downward and this library has no
sign-inverted reference concept. Other services need a pack of their own, though the core
needs nothing added in order to work with them, and framework adapters cover the OpenAI and
Anthropic tool formats rather than higher-level agent frameworks.

### Series

A quantity holds either one value or a series, and declarations behave identically
for both (`pip install quantity-guard[arrays]`), with the exception of comparison, which
returns an elementwise array rather than a single boolean, following numpy semantics.

### Interpretation

The opacity account rests on one unit pair in one domain, with one model
contradicting it, and it predicts an ordering over unit names that has not been tested
directly.

## Changes

The following behaviour changes may affect existing callers, and each is listed with the
reasoning behind it so that a caller relying on the previous behaviour can judge the impact.

A declared `quality` floor now refuses record whose grade is unstated, not only record that
is explicitly weaker, so callers already declaring a floor will see `QualityViolation` where
values carry no qualifier.

`VA` and `var` no longer convert to `W` or to each other. Both were previously defined as
`volt * ampere`, which made them silent aliases of the watt, so 100 VA converted to 100 W
without complaint. Converting between real, apparent, and reactive power requires a power
factor, which is a property of the circuit rather than of the units.

`mgd` is now `us_mgd`, on the US liquid gallon, with `imperial_mgd` named separately, since
UK water-resources practice writes "mgd" for million imperial gallons per day, a difference
of 20%. The `mgd` spelling still resolves, while the ambiguity does not.

A non-finite scalar magnitude is refused, because a NaN fails every comparison silently and
previously surfaced from the audit as unsourced rather than as invalid. A series keeps its
NaNs, that being how a time series marks a missing sample, and serialises them as `null`.

Scaling a datum-bearing value is refused, as is subtracting an elevation from a delta.

`require_explicit_unit` now applies to every input shape, including an object with no `unit`
key and a bare series, and the emitted JSON Schema's `required` list now matches what the
validator enforces.

Setting `enforcement="warn"` no longer rejects on the return path, whereas returns were
previously validated unconditionally, so warn mode raised from the one place in which it
undertakes not to.

`NTU`, `FNU`, and `pH_unit` are defined with distinct dimensions, so a turbidity cannot be
compared against a pH and the two turbidity standards cannot be interconverted.

The MCP proxy scopes its ledger to a client session, reset on `initialize` and bounded in
size. Quantities from one conversation previously triggered carry-over violations in
another and appeared in the error text shown to the second caller.

An unsupported JSON-RPC method now returns `-32601`, meaning method not found, rather than
`-32603`, which a client reads as the server itself having failed.

## Licence

MIT
