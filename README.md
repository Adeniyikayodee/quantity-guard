# quantity-guard

Typed physical quantities at the AI agent tool boundary.

Numbers that cross into and out of an agent's tools carry their unit, vertical datum,
coordinate reference system, and record quality as structured metadata that the runtime
enforces, instead of as prose the model is trusted to track.

## The problem

Language models handle units unreliably. They misjudge magnitude relationships between
units, they carry a value from one tool into another without noticing that the two
disagree on scale, and they occasionally state a quantitative result without calling the
tool that would have produced it.

Dimensional analysis alone does not catch the worst cases. A gage height of 12.4 ft above
a local station datum and a flood stage of 31.0 ft above NAVD88 are both lengths, so any
units library will subtract one from the other and return 18.6 ft, which is not a
freeboard, or anything else. The values are dimensionally compatible, and semantically
incompatible, and the resulting error is silent.

`quantity-guard` puts four checks at the boundary where tools are called:

1. Dimensional validation with automatic conversion where the conversion is well-defined,
   and refusal where it is not.
2. Reference frame validation for vertical datums, coordinate reference systems, and
   timezones, none of which are visible to dimensional analysis.
3. Carry-over detection, which catches a magnitude passed from one tool to another
   without its unit.
4. A provenance ledger, so that a number appearing in the final answer can be traced to
   the tool output it came from, or flagged if it came from nowhere.

## Install

```bash
pip install quantity-guard
```

## Guarding a server you did not write

Adopting this does not require rewriting your tools. `quantity-guard-mcp` wraps an
existing MCP server: it reads the server's tool list, merges in declarations from an
annotation file, re-advertises the tools with their units stated in the schema, and
validates calls on the way through.

```bash
quantity-guard-mcp --annotations water.toml -- python -m my_server
```

The annotation file supplies the physical types from outside, keyed by tool and
parameter. Only what you name is guarded, and everything else is forwarded untouched.

```toml
[tools.read_discharge]
returns = { unit = "cfs" }

[tools.runoff_depth.params]
discharge = { unit = "m**3/s" }
area = { unit = "km**2" }
```

Arguments are converted into the unit the upstream server already expects, so the server
needs no change. A model that sends `1250 cfs` to a parameter declared in m3/s causes the
upstream tool to receive `35.4`, which is the number it was always written for. Bare
numeric results come back labelled with their unit, which is what makes carry-over
detection work across a server the library knows nothing about.

`demo/usgs_server.py` is an ordinary server that states its units in prose and answers in
bare numbers. Running it behind the proxy and replaying four requests shows the whole
path:

```
2 schema  discharge x-unit = m**3/s
3 ok      {"value": 1250.0, "unit": "cfs"}
4 ERROR   [guard_violation] for `discharge` received the bare number 1250, which this
          tool reads as 1250 m3/s, but read_discharge.return returned 1250 cfs and no
          conversion was applied
5 ok      {"value": 0.1054, "unit": "mm / d"}
```

## Declaring a tool

```python
from quantity_guard import quantity_tool

@quantity_tool(
    params={
        "discharge": {"unit": "m**3/s", "description": "Observed discharge."},
        "area": {"unit": "km**2", "description": "Contributing drainage area."},
    },
    returns={"unit": "mm/day"},
)
def runoff_depth(discharge, area):
    """Depth-equivalent runoff over the contributing area."""
    return discharge / area
```

The body receives `Q` values, so it is written in terms of physical quantities and the
arithmetic carries units through. Callers may pass a bare number in the declared unit, a
string such as `"1250 cfs"`, or an object of the form
`{"value": 1250, "unit": "cfs", "quality": "provisional"}`, and all three normalise to the
declared unit before the body runs.

```python
runoff_depth(discharge="1250 cfs", area="29000 km**2")
# Q(0.105423 mm / day)
```

A dimensionally wrong argument is rejected rather than coerced:

```python
runoff_depth(discharge="12.4 ft", area="29000 km**2")
# DimensionalityError: expected a quantity in m**3/s ([length]^3 [time]^-1),
# received 12.4 ft ([length]); these are different physical quantities and no
# conversion exists
```

## Schema generation

`json_schema()` emits an MCP tool definition extended with `x-unit`, `x-datum`, `x-crs`,
and `x-tz`, so the model reads the expected physical type before it calls:

```python
runoff_depth.json_schema()
```

```json
{
  "name": "runoff_depth",
  "description": "Depth-equivalent runoff over the contributing area.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "discharge": {
        "description": "Observed discharge.",
        "x-unit": "m**3/s",
        "oneOf": [
          {"type": "number", "description": "magnitude in m**3/s"},
          {"type": "object", "properties": {"value": {"type": "number"}, "unit": {"type": "string"}}},
          {"type": "string", "description": "quantity with unit, e.g. \"1.5 m**3/s\""}
        ]
      }
    },
    "required": ["discharge", "area"],
    "additionalProperties": false
  }
}
```

## Vertical datums

Datums are named reference frames. Two quantities on different datums cannot be
differenced or compared, and a conversion between them requires an offset that is
registered explicitly, because the true offset varies with location and cannot be
inferred.

```python
from quantity_guard import Q, datums
from quantity_guard.packs.water import register_station

register_station("07374000", Q(1.5, "ft", datum="NAVD88"))

stage = Q(12.4, "ft", datum="GAGE:07374000")
flood_stage = Q(31.0, "ft", datum="NAVD88")

flood_stage - stage
# DatumMismatch: cannot difference an elevation on NAVD88 against one on
# GAGE:07374000, since both are in compatible units but measured from different
# references

flood_stage - stage.to_datum("NAVD88")
# Q(17.1 ft)
```

Differencing two elevations on a shared datum yields a delta that carries no datum, which
is what makes freeboard arithmetic well-defined while leaving the sum of two absolute
elevations rejected.

Where no offset has been registered, the conversion fails rather than guessing:

```python
Q(31.0, "ft", datum="NAVD88").to_datum("NGVD29")
# DatumConversionUnavailable: no registered offset from 'NAVD88' to 'NGVD29'.
# This conversion depends on location and will not be guessed
```

## Carry-over between tools

A bare number entering a tool is read in that tool's declared unit, which is the contract
the schema states. That contract breaks when a model takes a magnitude from one tool's
output and passes it to another without the unit attached, which is the arithmetic behind
most order-of-magnitude errors in agent transcripts.

Inside a session the ledger makes this detectable. If an incoming bare number equals a
value an earlier tool returned in a different but dimensionally compatible unit, the
conversion was skipped:

```python
with session():
    discharge = read_discharge("07374000")   # returns 1250 cfs
    runoff_depth(discharge=1250, area=29000)
# UnconvertedCarryOver: received the bare number 1250, which this tool reads as
# 1250 m³/s, but read_discharge.return returned 1250 cfs and no conversion was
# applied; resend the value with its original unit, as
# {"value": 1250, "unit": "cfs"}
```

The unguarded version of that call returns a runoff depth 35 times too large, and nothing
about the result looks wrong.

For tools where no bare number is ever acceptable, `require_explicit_unit` refuses them
outright rather than relying on the ledger.

## Series

A gage record is a series, not a number, so a quantity holds either. The declarations are
unchanged; only the magnitude differs. Install with `pip install quantity-guard[arrays]`.

```python
Q([980.0, 1250.0, 1640.0], "cfs").to("m**3/s")
# Q([3 values, 27.7513 to 46.4406] m³/s)
```

Reference metadata applies to the whole series, so a datum shift, a quality flag, or a
dimensionality refusal behaves exactly as it does for one value. Carry-over detection and
the answer audit both compare a single magnitude, so a series is recorded in the ledger
and in the manifest but is never matched against; `Session.scalar_outputs` is what those
checks read.

## Tool definitions for your framework

The library's own schema is MCP-shaped. The same declarations are emitted in the OpenAI
and Anthropic tool formats, with the physical metadata riding along in the parameter
schemas, since both providers pass unknown keys through to the model.

```python
from quantity_guard import toolbox

box = toolbox([runoff_depth])
box.schemas("openai")      # [{"type": "function", "function": {...}}]
box.schemas("anthropic")   # [{"name": ..., "input_schema": {...}}]

payload = box.invoke("runoff_depth", {"discharge": "1250 cfs", "area": 29000})
box.result_message("openai", call_id, payload)
```

A rejected call comes back as an error result rather than an exception, so the repair text
reaches the model instead of the process.

## Measuring before enforcing

`enforcement="warn"` validates without rejecting: the call proceeds on the raw value, and
the violation is recorded. It exists so a team can find out what enforcement would cost
before paying it.

```python
with session() as ledger:
    ...
    print(ledger.enforcement_report())
```

```
2 of 3 tool calls would have been blocked:
  2x dimensionality_error
      runoff.discharge: expected a quantity in m**3/s ([length]^3 [time]^-1),
      received 12.4 ft ([length]); these are different physical quantities
```

## Record quality

Quality flags propagate through arithmetic, taking the weakest input, so a result
computed from provisional record is itself marked provisional. USGS single-letter codes
are accepted directly.

```python
Q(1250, "cfs", quality="P") + Q(90, "cfs", quality="A")
# Q(1340 cfs (provisional))
```

A tool may set a floor, in which case input below it is refused:

```python
@quantity_tool(params={"discharge": {"unit": "m**3/s", "quality": "approved"}})
def publish_annual_summary(discharge):
    ...
```

## Timezones

A parameter declaring `tz` accepts only timezone-aware timestamps, which matters because
USGS publishes gage records in local standard time while models default to UTC.

```python
@quantity_tool(params={"observed_at": {"tz": "America/Chicago"}})
def lookup(observed_at):
    ...

lookup(observed_at="2026-08-14T09:30:00")
# TimezoneError: timestamp 2026-08-14T09:30:00 is timezone-naive, and gage records
# are published in local standard time while models default to UTC
```

## Provenance and unsourced numbers

Inside a session, guarded tools record every quantity crossing their boundary. Auditing
an answer then checks each numeric literal in the text against that ledger.

```python
from quantity_guard import session

with session() as s:
    peak = forecast_peak_stage(station="07374000")
    audit = s.audit_answer(
        "The river is forecast to crest at 17.1 ft of freeboard, "
        "with a peak discharge of 4200 cfs."
    )

audit.ok        # False
audit.unsourced # [NumberClaim(text='4200 cfs', status='unsourced', ...)]
```

Three verdicts are possible for each number. A value matching a recorded output is
`sourced`. A value matching nothing is `unsourced`, which is the signature of a figure
produced without calling the tool. A value whose magnitude matches a recorded output but
whose stated unit is dimensionally incompatible with it is `unit_mislabelled`, which
catches a correct number reported in the wrong unit.

Values computed outside a guarded tool can be registered so the audit accepts them:

```python
s.record_derived(Q(17.1, "ft"), note="freeboard")
```

`s.manifest()` returns the full ledger, including every quantity, its unit, datum, and
quality, which is enough to re-run the session and check the numbers independently.

## Errors are written for the model

Every violation carries a `repair()` string stating what was wrong and what to send
instead, and `GuardedTool.invoke()` returns it as an MCP tool error rather than raising,
so a rejected call stays in the conversation where the model can correct it.

```python
runoff_depth.invoke({"discharge": {"value": 12.4, "unit": "ft"}, "area": 29000})
```

```python
{
  "isError": True,
  "content": [{"type": "text", "text": "[dimensionality_error] for `discharge` expected a quantity in m**3/s ..."}],
  "code": "dimensionality_error",
  "field": "discharge",
}
```

## Reading real data

`quantity_guard.packs.usgs` retrieves from USGS Water Services and keeps what the service
already publishes. The API states a unit code on every variable, a qualifier marking the
record provisional or approved, an explicit UTC offset on each timestamp, and a site record
giving the gage datum and the reference it is measured from. Clients normally parse the
number and drop the rest.

```python
from quantity_guard.packs import usgs

site, values = usgs.reading("07374000")
values["00060"].value      # Q(234000 ft³/s (provisional))
values["00065"].value      # Q(7.73 ft (GAGE:07374000, provisional))
values["00060"].observed_at.utcoffset()   # the offset the service stamped, not a guess
```

Reading the site record registers the station datum, so a gage height comes back on the
gage's own reference and differencing it against an absolute elevation is refused rather
than quietly wrong. Network access goes through a replaceable `fetch`, and the tests run
against recorded responses; `pytest -m live` checks them against the service.

## Answering where a number came from

The audit issues one of five verdicts per figure. `sourced` matched a recorded output.
`derived` is a sum or difference of recorded outputs, which is what a model produces when
it adds a station datum to a stage by hand. `quoted` was repeated back from the question,
such as a forecast horizon the asker supplied. `unsourced` matched nothing, and
`unit_mislabelled` matched a magnitude but contradicted its unit. Only the last two make
`audit.ok` false.

The first two verdicts exist because the audit was measured, not assumed. Across 583
correct answers from three models it flagged 34% of them, rising to 98% on one task, which
is an unusable rate for a check meant to be trusted. Every cause turned out to be a
legitimate number: values the model had derived, values it had quoted back from the
question, a derived value written without a unit, and a figure rounded from 17.1 to 17.
Re-running the three worst tasks over 288 fresh transcripts puts the rate at 0%.

| task | before | after |
|---|---|---|
| unsourced_peak | 98% | 0% |
| hard_freeboard | 72% | 0% |
| freeboard | 31% | 0% |

Derivation follows sums and differences of like dimensions only, and one step deep.
Allowing products and quotients as well was measured to accept 53% of randomly chosen
numbers on a six-output ledger, which would leave the audit unable to detect anything.
With the restriction, a random number is accepted 2.3% of the time on a three-output
ledger and 6.8% on a six-output one, and a fabricated peak discharge is still caught.

The audit answers provenance, not correctness. A freeboard of 18.6 ft computed as
31.0 − 12.4 is `derived`, because it genuinely came from two recorded outputs; that it used
the wrong operation is a physics error, and catching it is the datum check's job at the
tool boundary.

## Domain packs

`quantity_guard.packs.water` supplies specifications for surface water work, covering
discharge, gage height, elevation, water temperature, precipitation, and drainage area,
along with `register_station()` for binding a gage to its local datum.

```python
from quantity_guard.packs.water import DISCHARGE, GAGE_HEIGHT, station_spec

@quantity_tool(params={"q": DISCHARGE, "stage": station_spec("07374000")})
def rating_residual(q, stage):
    ...
```

