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

