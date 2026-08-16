# fix.md

Remediation log for the red-team review of quantity-guard 0.6.2.

Findings are worked one at a time, in severity order. Each entry records the defect, the
reproduction, the change, and the regression test that pins it. Numbering follows the
review; an entry appears here only once its fix is applied and the suite is green.

Baseline at the start of this log: **141 passed, 2 deselected**.

---

## Fix 1 — scaling a quantity silently stripped its vertical datum

**Severity:** critical. Defeats the library's headline guarantee.

**Where:** `src/quantity_guard/quantity.py`, `Q._scale` and `Q.__sub__`.

### Defect

`_scale` propagated `crs` and `quality` onto the result but not `datum`. Multiplying or
dividing an absolute elevation by a plain number therefore returned a datum-free value,
and a datum-free value passes every downstream check.

```python
crest = Q(31.0, "ft", datum="NGVD29")   # levee crest, 1970s survey
wsel  = Q(26.5, "ft", datum="NAVD88")   # forecast water surface

crest - wsel        # DatumMismatch          the guard working
(crest * 1) - wsel  # Q(4.5 ft (NAVD88))     the guard gone
(crest / 1) - wsel  # Q(4.5 ft (NAVD88))
(crest * 1) + wsel  # Q(57.5 ft (NAVD88))    a sum of two elevations, which __add__ forbids
```

4.5 ft is precisely the wrong answer the project's own benchmark is built around
(README: *"Seven of eleven models report 4.5 ft of freeboard where 4.06 ft is correct"*).
Any unit-bearing intermediate step — scaling a stage by a rating coefficient, a `* 1.0`
normalisation — laundered an absolute elevation into a delta that then compared and
differenced freely against any other datum.

A second defect in the same area: `__sub__` derived the result datum with
`self.datum or other.datum`, so `delta - elevation` came back **labelled as an absolute
elevation**:

```python
Q(5.0, "ft") - crest   # Q(-26 ft (NGVD29))
```

−26 ft is not an elevation on NGVD29. The expression has no physical meaning at all.

### Change

`_scale` now refuses any operand carrying a datum, by a scalar exactly as it already did
by another `Q`. Twice an elevation is not an elevation, so the operation is rejected
rather than silently demoted to a delta. The `Q * Q` and `Q * scalar` paths are unified
into one guard ahead of the branch.

`__sub__` gained an explicit rule and now takes the reference from the left operand only:

| left | right | result |
|---|---|---|
| elevation on X | elevation on X | delta, no datum |
| elevation on X | elevation on Y | `DatumMismatch` |
| elevation on X | delta | elevation on X |
| delta | elevation on Y | `DatumMismatch` — new |

The last row was the leak. `datum = None if other.datum else self.datum` replaces the
`or` expression, which also makes the first and third rows fall out directly rather than
by coincidence.

Conversion is unaffected: `Q.to` and `Q.to_datum` go through `pint`/`replace` and keep
carrying the datum, as they did before.

### Verification

```
crest - wsel                 -> BLOCKED DatumMismatch
(crest*1) - wsel             -> BLOCKED DatumMismatch
(crest/1) - wsel             -> BLOCKED DatumMismatch
(crest*1) + wsel             -> BLOCKED DatumMismatch
delta - crest                -> BLOCKED DatumMismatch
crest - delta  [legit]       -> Q(26 ft (NGVD29))
crest + delta  [legit]       -> Q(36 ft (NGVD29))
same-datum diff [legit]      -> Q(2.9 ft)
delta * 2      [legit]       -> Q(10 ft)
crest.to(m)    [legit]       -> Q(9.4488 m (NGVD29))
```

### Tests

Added to `tests/test_quantity.py`:

- `test_scaling_an_elevation_cannot_launder_its_datum` — covers `q * 1`, `q / 1`, and
  `1 * q` (the `__rmul__` path), both the scaling itself and the subsequent cross-datum
  difference.
- `test_scaling_a_delta_is_still_allowed` — guards against over-correction.
- `test_subtracting_an_elevation_from_a_delta_is_refused`
- `test_a_delta_may_be_subtracted_from_an_elevation` — the legitimate mirror case.

**Suite: 145 passed, 2 deselected.** No existing test scaled a datum-bearing value, so
nothing regressed.

### Note for the README

`## Vertical datums` states that differencing two elevations on a shared datum yields a
delta "while leaving the sum of two absolute elevations refused". That is still accurate,
and now also true by construction rather than reachable around. No wording change needed.

---

## Fix 2 — `enforcement="warn"` rejected calls on the return path

**Severity:** critical. Breaks the documented adoption path.

**Where:** `src/quantity_guard/tool.py`, `GuardedTool._validate_result`.

### Defect

`_coerce` honoured all three enforcement modes for arguments. `_validate_result` honoured
only `off`, calling `Spec.coerce` directly for everything else. A tool in `warn` mode
therefore raised whenever its *return* value violated its declaration — and because the
raise happened outside `_coerce`, the violation was never appended to
`session.violations` either.

```python
@quantity_tool(params={"q": {"unit": "m**3/s"}}, returns={"unit": "m**3/s"},
               enforcement="warn")
def f(q):
    return Q(3.0, "ft")

f(5.0)   # raises DimensionalityError
         # ledger.violations: []
```

README: *"`enforcement="warn"` validates without rejecting: the call proceeds on the raw
value and the violation is recorded."* Neither half held. This is the mode recommended
for measuring the cost of enforcement before paying it, so the failure landed exactly
where a team is least expecting it: switch `warn` on to take a measurement, and the tool
starts throwing instead — with nothing in `enforcement_report()` to explain why.

The same gap covered the wrong-shape case: a tool declaring a mapping of returns but
producing a non-dict raised a bare `GuardViolation` regardless of mode.

### Change

Introduced `_decline(violation, field, fallback, session)`, the single place that decides
what a violation means under the current mode: raise under `strict`, or record a
`WouldBlock` and hand back the fallback under `warn`.

`_validate_result` now takes the session and routes every return check through
`_coerce_result`, which mirrors the input path — validate, and on violation fall back to
`_lenient` via `_decline`. The wrong-shape branch routes through `_decline` too, with the
unvalidated result as its fallback.

`_coerce` was collapsed onto the same helper, so the input and output paths can no longer
drift apart. Carry-over and `sourced` checks stay input-only, which is correct: both ask
where a value *came from*, and a return value's origin is the tool itself.

### Verification

```
warn wrong-dim return    -> Q(3 ft)  violations=1
    bad_return.return: dimensionality_error
  1 of 1 tool calls would have been blocked:
warn wrong shape         -> Q(3 ft)  violations=1
    bad_shape.return: guard_violation
  1 of 1 tool calls would have been blocked:
strict                   -> raises DimensionalityError (correct)
```

### Tests

Added to `tests/test_adapters.py`, beside the existing warn-mode coverage (which tested
only the input path):

- `test_warn_mode_does_not_reject_on_the_return_path` — asserts the raw value comes back,
  the violation is recorded against field `return`, and it reaches `enforcement_report()`.
- `test_warn_mode_tolerates_a_return_of_the_wrong_shape`
- `test_strict_mode_still_rejects_on_the_return_path` — guards against over-correction.

**Suite: 148 passed, 2 deselected.**

---

## Fix 3 — `require_explicit_unit` was enforced on one input shape out of four

**Severity:** high. A declared check was bypassable, and the emitted schema advertised a
constraint the validator did not apply.

**Where:** `src/quantity_guard/spec.py`, `Spec._to_quantity` and `Spec.json_schema`.

### Defect

`_to_quantity` dispatches on the shape of the incoming value. The
`require_explicit_unit` check lived only in the `isinstance(value, (int, float))` branch,
so three of the four accepted shapes walked past it. The dict branch in particular did
`value.get("unit") or self.unit`, which quietly substitutes the declared unit whenever
the caller's unit is absent, empty, or null.

```python
strict_flow(1250)                          # BLOCKED        correct
strict_flow({"value": 1250})               # Q(1250 m³/s)
strict_flow({"value": 1250, "unit": ""})   # Q(1250 m³/s)
strict_flow({"value": 1250, "unit": None}) # Q(1250 m³/s)
strict_flow([1250, 1300])                  # Q([2 values] m³/s)
```

The parameter exists precisely so that a magnitude cannot arrive without a stated unit.
Wrapping the same bare magnitude in an object defeated it.

Compounding this, `json_schema()` emitted `"required": ["value", "unit"]` on the object
variant unconditionally — including for parameters that accept unit-less objects. The
schema shown to the model therefore described a stricter contract than the one enforced,
in the direction that penalises a model for following it.

### Change

Both remaining shapes now honour the declaration:

- **dict** — refused when `unit` is missing, empty, or null. The message distinguishes
  the two cases ("left it out" vs "left it empty") so the repair text is actionable.
- **list / tuple / array** — refused outright; a bare series is a bare magnitude repeated.

The string branch is untouched: `Q.parse` already fails on a string with no unit, and an
object serialised into a string is re-entered through the dict branch, so it inherits the
new check.

`json_schema()` now emits `required: ["value", "unit"]` only when
`require_explicit_unit` is set, and `["value"]` otherwise. The schema and the validator
now agree in both directions.

### Verification

```
 require_explicit_unit=True
  bare 1250                      -> BLOCKED MissingUnit
  {value:1250}                   -> BLOCKED MissingUnit
  {value:1250,unit:""}           -> BLOCKED MissingUnit
  {value:1250,unit:None}         -> BLOCKED MissingUnit
  [1250,1300]                    -> BLOCKED MissingUnit
  {value:1250,unit:cfs}  [legit] -> Q(35.3961 m³/s)
  "1250 cfs"             [legit] -> Q(35.3961 m³/s)
 require_explicit_unit=False
  bare 1250              [legit] -> Q(1250 m³/s)
  {value:1250}           [legit] -> Q(1250 m³/s)
  [1250,1300]            [legit] -> Q([2 values, 1250 to 1300] m³/s)
 schema required[] now matches the validator
   require_explicit_unit=True  -> required=['value', 'unit']
   require_explicit_unit=False -> required=['value']
```

### Tests

Added to `tests/test_tool.py`:

- `test_explicit_unit_is_required_on_every_input_shape` — the five evasions above,
  plus the unit-bearing object as a control.
- `test_a_unitless_object_is_still_accepted_when_no_unit_is_required` — guards against
  over-correction; the permissive default is the documented contract.
- `test_the_object_variant_declares_only_the_keys_it_enforces` — pins the schema to the
  validator in both directions.

**Suite: 151 passed, 2 deselected.**

---

## Fix 4 — the quality gate was vacuous, and the USGS pack discarded the codes that matter

**Severity:** high, and the one a hydrologist would raise loudest. Two defects that
compounded: the core check waved through unflagged record, and the retrieval pack made
the most consequential readings arrive unflagged.

**Where:** `src/quantity_guard/spec.py` (`_coerce_quantity`),
`src/quantity_guard/registry.py` (`QUALITY_ALIASES`, `normalize_quality`),
`src/quantity_guard/packs/usgs.py` (`_quality`).

### Defect (a) — an unflagged value satisfied any floor

`if self.quality and quantity.quality:` — the check ran only when the incoming value
happened to carry a flag. Dropping the flag satisfied the requirement.

```python
approved_only({"value": 10, "unit": "m**3/s", "quality": "P"})  # BLOCKED   correct
approved_only({"value": 10, "unit": "m**3/s"})                  # Q(10 m³/s)
approved_only(10)                                               # Q(10 m³/s)
```

This is backwards from the library's stance everywhere else: a bare *number* is refused
under `require_explicit_unit`, but a bare *quality* was accepted as approved.

### Defect (b) — NWIS condition codes were dropped

`_quality` scanned for `P`, `e`, `A` and returned `None` for everything else:

```
['Ice'] -> None    ['Eqp'] -> None    ['Bkw'] -> None
['Fld'] -> None    ['Dis'] -> None    ['***'] -> None    ['E'] -> None
```

Ice-affected, backwater-affected, and equipment-malfunction readings came back
indistinguishable from clean record — and by (a) then cleared an `approved` gate. These
are exactly the qualifiers that matter: an ice-affected discharge can be wrong by a large
margin while looking entirely plausible, which is the failure class this library exists
to catch. Uppercase `E` (estimated, standard in NWIS daily values) was dropped too.

### Defect (c) — an unknown flag raised a raw `ValueError`

`normalize_quality` raised `ValueError`, not a `GuardViolation`, so it escaped
`invoke()`'s repair path and `warn` mode entirely. It was also exact-match on case, so
publisher word forms in any other casing failed.

### Change

**(a)** The floor now refuses a value carrying no flag, with a message that says why and
what to send. A grade that is unknown cannot clear a stated bar, or the bar is
satisfiable by discarding information.

**(b)** NWIS condition codes moved into `QUALITY_ALIASES` alongside the review-status
codes, so they are understood library-wide rather than only inside the pack — a model
forwarding `"quality": "Ice"` from a raw payload is now graded, not rejected as unknown.
`_quality` reduces to "map every recognised qualifier, take the weakest", which also
means `["A", "Ice"]` correctly grades as `unverified`: approved record *of* an
ice-affected measurement is not approved-quality data. Unrecognised codes are ignored
rather than raising, since NWIS carries footnote codes that say nothing about quality.

Condition-code grades: `Ice`/`Bkw`/`Eqp`/`Fld`/`Dis`/`Mnt`/`***` → `unverified`;
`Rat`/`Ssn`/`Dry`/`ZFl` → `estimated`.

**(c)** `normalize_quality` raises `QualityViolation`. Single-letter agency codes stay
case-significant (USGS distinguishes `e` and `E` elsewhere in its vocabulary); word forms
are matched case-insensitively.

### Verification

```
a) quality floor
  quality=P                          -> BLOCKED QualityViolation
  NO quality flag                    -> BLOCKED QualityViolation
  bare number                        -> BLOCKED QualityViolation
  quality=A          [legit]         -> Q(10 m³/s (approved))
  quality=Good       [legit]         -> Q(10 m³/s (approved))
  quality=good       [legit]         -> Q(10 m³/s (approved))
  quality=banana                     -> BLOCKED QualityViolation (was raw ValueError)

b) USGS qualifier mapping
   ['P'] -> 'provisional'      ['Ice']      -> 'unverified'
   ['A'] -> 'approved'         ['A','Ice']  -> 'unverified'
   ['E'] -> 'estimated'        ['Eqp']      -> 'unverified'
   ['e'] -> 'estimated'        ['ZFl']      -> 'estimated'
   []    -> None               ['Zz']       -> None  (footnote, ignored)

c) end to end
   ice-affected retrieval  -> Q(234000 ft³/s (unverified))
   ..into an approved gate -> BLOCKED QualityViolation
```

### Tests

- `tests/test_usgs.py::test_qualifiers_become_quality_flags` — extended from 4 cases to
  20, covering both code families, the weakest-wins rule across families, and ignored
  footnote codes.
- `tests/test_usgs.py::test_an_ice_affected_reading_cannot_clear_an_approved_gate` —
  end to end, from the recorded payload through to the tool boundary.
- `tests/test_tool.py::test_an_unflagged_record_cannot_satisfy_a_quality_floor` — all
  four input shapes.
- `tests/test_tool.py::test_a_tool_with_no_quality_floor_still_takes_unflagged_record` —
  guards against over-correction; only a declared floor triggers the requirement.

**Suite: 169 passed, 2 deselected.**

### Note for the README

The declaration table describes `quality` as *"weakest acceptable record, so a tool can
decline provisional data"*. That now also covers record of unstated grade, which is a
behaviour change for anyone already declaring a floor: a caller passing values with no
qualifier will start being refused. Worth a sentence in the table and a line in the
release notes.

---

## Fix 5 — `ea.reading()` raised on most real Environment Agency stations

**Severity:** high. The documented entry point for the UK pack failed on the majority of
the network.

**Where:** `src/quantity_guard/packs/ea.py`, `station`.

### Defect

Registration of the station's own datum was nested inside `if offset is not None`, but
`Station.datum_name` was returned unconditionally and `readings()` stamped it onto every
`mASD` level. For a station publishing no `datumOffset`, the name was never registered,
so `Q.__post_init__` rejected it.

Measured against the live service before the fix:

```
ea.reading() on 40 real EA stations -> 22 ok, 18 raised
  FAIL 1029TH  datumOffset=None  DatumMismatch: unknown datum 'GAUGE:1029TH'
  FAIL 2067    datumOffset=None  DatumMismatch: unknown datum 'GAUGE:2067'
  FAIL E8266   datumOffset=None  DatumMismatch: unknown datum 'GAUGE:E8266'
  ...
```

Sampling the live station list, **37 of 287 stations (13%) publish `datumOffset`**, so
this was the common path, not an edge case.

### Change

The two facts were conflated. A station's zero is a real reference frame whether or not
its height above Ordnance Datum has been surveyed and published — that is what lets a
level be *labelled* with the frame it was measured from. The offset is a separate,
additional fact that lets it be *converted* to ODN. Only the second is conditional.

Registration of the datum name now happens whenever `register` is set; registration of
the offset stays gated on the offset being published.

The result for an offset-less station is the honest one: the level comes back labelled
`GAUGE:X9999`, so differencing it against an absolute elevation is still refused, and
`to_datum("ODN")` raises `DatumConversionUnavailable` rather than returning a guess.

### Verification

The same 40 live stations, after the fix:

```
ea.reading() on 40 real EA stations -> 35 ok, 5 raised  (5 convertible to ODN)
  FAIL 2406TH        TimeoutError
  FAIL 4615TH        TimeoutError
  FAIL 43165         TimeoutError
  FAIL 2020          HTTPError 502: Bad Gateway
  FAIL 055003_TG 316 InvalidURL: URL can't contain control characters
```

**Zero `DatumMismatch` raises remain**, against 18 before. Four of the five residual
failures are service transients. The fifth was a separate defect, fixed below.

Fixture behaviour:

```
with datumOffset    -> Q(0.117 m (GAUGE:E21136)) | to ODN: Q(6.417 m (ODN))
without datumOffset -> Q(0.117 m (GAUGE:X9999))  | to ODN: BLOCKED DatumConversionUnavailable
```

### Tests

`tests/test_ea.py::test_a_station_with_no_published_offset_is_still_readable` — reads a
station with `datumOffset` removed, asserts the level is labelled with the station frame,
and asserts the ODN conversion is still refused.

**Suite: 170 passed, 2 deselected.**

### Fix 5b — station references were interpolated into the URL unescaped

Found while verifying the above, in the same function, so closed with it.

`station` and `readings` built their URLs as `f"{BASE}/id/stations/{reference}"`. Three of
287 sampled live references contain a space — `055003_TG 316`, `067027_TG 127`,
`055021_TG 305` — which raises `InvalidURL` in `urllib` before any request is made.

Percent-encoding is not the fix. The service substitutes an underscore for the space in
its own `@id` and answers only to that spelling:

```
/id/stations/055003_TG_316    -> 200
/id/stations/055003_TG%20316  -> 500
/id/stations/055003_TG+316    -> 404
```

Added `_ref()`, which applies that substitution and then escapes normally, so an
unexpected character fails as a clean 404 rather than a client-side exception. Both call
sites use it.

Live, after:

```
'055003_TG 316'  -> ok  datum=GAUGE:055003_TG 316  readings=1  Q(0.201 m (...))
'067027_TG 127'  -> ok  datum=GAUGE:067027_TG 127  readings=0
'E21136'         -> ok  datum=GAUGE:E21136         readings=1  Q(0.139 m (...))
```

Test: `tests/test_ea.py::test_station_references_are_spelled_as_the_service_spells_them`,
four cases including both live space-bearing references and surrounding whitespace.

**Suite: 207 passed, 2 deselected.**

---

## Fix 6 — Environment Agency quality was read from a field that does not exist

**Severity:** medium. Dead code presenting as coverage.

**Where:** `src/quantity_guard/packs/ea.py`, `readings`.

### Defect

The pack read `measure.get("qualityControl")`. That key appears on neither a measure nor
a reading, on any endpoint. Verified live:

```
measure keys:  ['@id','datumType','label','latestReading','notation','parameter',
                'parameterName','period','qualifier','station','stationReference',
                'unit','unitName','valueType']
reading keys:  ['@id','date','dateTime','measure','value']
```

The only `qual`-named field is `qualifier`, whose value is `"Stage"` — the measurement
position, not a record grade. So EA quality was permanently `None`, and the
`Good`/`Unchecked`/`Estimated`/`Suspect`/`Missing` entries in `QUALITY_ALIASES` were
unreachable by any code path, while the README stated *"USGS single letters and
Environment Agency words are both understood"*.

### Change

Replaced the dead read with `_quality(measure, latest)`, which looks for a `quality` key
on the reading first and the measure second — where the EA archive exports place it — and
returns `None` otherwise.

The docstring now records the finding directly: the real-time API states no grade, so
`None` is a true statement about the source rather than a gap in the pack, and the alias
entries are live for archive rows fed through the same pack.

### Verification

```
live API shape (no grade published) -> Q(0.117 m (GAUGE:E21136))  quality = None
archive row w/ quality=Unchecked    -> Q(0.117 m (GAUGE:E21136, provisional))
grade on the measure                -> Q(0.117 m (GAUGE:E21136, unverified))
```

### Tests

`tests/test_ea.py::test_a_published_quality_word_reaches_the_alias_table` — asserts
`None` for the live payload shape, and correct grading when the word is present on either
object.

**Suite: 171 passed, 2 deselected.**

### Note for the README

*"Quality codes are mapped per agency. USGS single letters and Environment Agency words
are both understood"* is now true of the code but still overstates what the live UK
service supplies. Suggested amendment: note that the EA real-time API publishes no grade,
so UK quality enforcement applies to archive data.

---

## Fix 7 — unmapped unit codes aborted the whole station read, and "deg C" was unmapped

**Severity:** medium-high. Water temperature was unretrievable from USGS, and a single
unknown sensor unit cost the caller every reading at the site.

**Where:** `src/quantity_guard/registry.py` (`_EXTRA_UNITS`),
`src/quantity_guard/packs/usgs.py` (`UNIT_CODES`, `unit_for`, `instantaneous`),
`src/quantity_guard/packs/ea.py` (`UNIT_NAMES`, `readings`).

### Defect

`unit_for` was an exact, case-sensitive dict lookup. NWIS publishes `"deg C"`; the table
was keyed `"deg c"`. Confirmed live on site 01646500:

```
'00010' | 'deg C' | Temperature, water
usgs.instantaneous(..., parameters=("00010",))
  -> ValueError: unmapped USGS unit code 'deg C'
```

Both packs raised from inside their per-series loop, with no handler, so **one unmapped
parameter aborted the entire station read** — losing discharge and stage to an unknown
turbidity unit.

Coverage was thin against what the services actually publish. Live census:

- USGS 01646500: `FNU` unmapped. Also missing: `%`, `std units` (pH), `ac-ft`,
  `ft/sec`, `m3/s`, `tons/day`, `ug/l`, `NTU`.
- EA, 500 measures sampled: `mASD` 206, `m` 198, **`---` 38**, `mAOD` 28, `m3/s` 22,
  `V` 2, `m/s` 2, `mm` 2, `%` 1, `deg` 1 — five of the ten unmapped.

### Change

**Lookup.** `unit_for` tries the exact spelling first, so a runtime addition to
`UNIT_CODES` is still honoured, then falls back to a case-insensitive comparison.

**Coverage.** Both tables extended to everything observed on the live services.

**Index units.** `NTU`, `FNU`, and `pH_unit` are defined in the registry, each with its
own dimension. Turbidity is not a pH, and NTU (white-light) and FNU (near-infrared) are
different instrument standards that the services publish side by side — giving them one
shared dimension would let them interconvert silently, which is the same class of error
the datum registry prevents one level up. They now refuse.

**mBDAT deliberately unmapped.** The EA publishes some levels as metres *below* datum.
Mapping it to metres on the station datum would invert the sign of every reading, and the
library has no downward-positive reference concept. Refusing is safer than mislabelling;
the table says so in a comment.

**Skip, don't abort.** An unmapped unit now skips its own series and emits a
`UserWarning` naming what was dropped. Losing a water-quality sensor is a better outcome
than losing the flood-relevant values alongside it — provided the omission is visible,
which the warning makes it.

### Verification

Live, site 01646500 — the site that previously raised on `deg C`:

```
00010 Temperature            Q(24.3 °C (approved))          2019-10-01
00060 Streamflow             Q(3010 ft³/s (provisional))    2026-08-15
00065 Gage height            Q(3.03 ft (provisional))       2026-08-15
00095 Specific conductance   Q(406 µS/cm (approved))        2019-10-01
63680 Turbidity              Q(6.2 FNU (approved))          2019-05-27
```

All five retrieve; no warnings, because nothing is unmapped. Index units refuse to
cross-convert:

```
NTU->FNU:      refused (DimensionalityError)
NTU->pH_unit:  refused (DimensionalityError)
pH_unit->percent: refused (DimensionalityError)
```

Note the observation dates in that output — they are the subject of Fix 8.

### Tests

- `tests/test_usgs.py::test_unit_codes_are_matched_case_insensitively` — 11 cases
  including all three casings of the temperature code.
- `tests/test_usgs.py::test_an_unmapped_parameter_does_not_cost_the_whole_station`
- `tests/test_usgs.py::test_index_units_do_not_interconvert`
- `tests/test_ea.py::test_unit_names_the_live_service_publishes_are_mapped` — 15 cases.
- `tests/test_ea.py::test_metres_below_datum_is_refused_rather_than_mislabelled`
- `tests/test_ea.py::test_an_unmapped_measure_does_not_cost_the_whole_station`

**Suite: 203 passed, 2 deselected.**

---

## Fix 8 — no concept of staleness: a 2019 reading was returned as "latest"

**Severity:** medium-high. Timeliness is metadata that matters as much as unit, and it
was the one piece the library discarded.

**Where:** `src/quantity_guard/packs/usgs.py` (`Observation`, `instantaneous`,
`reading`), `src/quantity_guard/packs/ea.py` (`Reading`, `readings`, `reading`).

### Defect

Both packs took the last point the service held, unconditionally. `observed_at` was
captured on the result object but never reached the `Q`, never entered the ledger, and no
check anywhere consulted it.

The service returns the last value for each parameter *independently*, so one response
mixes ages freely. Observed live at 01646500 (Potomac at Little Falls, a flood-forecast
gauge), from a single call:

```
00060 Streamflow            3010 ft3/s   2026-08-15   <- current
00065 Gage height           3.03 ft      2026-08-15   <- current
00010 Temperature           24.3 degC    2019-10-01   <- seven years old
00095 Specific conductance  406 uS/cm    2019-10-01   <- seven years old
63680 Turbidity             6.2 FNU      2019-05-27   <- seven years old
```

A function documented as *"Latest instantaneous values"* handed back a seven-year-old
temperature as current, and every guard passed it: the magnitude was real, the unit
right, the quality flag genuine. For a library whose thesis is that discarded metadata
causes silent errors, this was the metadata it discarded.

The EA pack has the same shape — a station with a failed sensor keeps serving that
sensor's last good value indefinitely.

### Change

`Observation` (USGS) and `Reading` (EA) gained `age` and `is_stale(max_age)`, so the
question is answerable at all.

`instantaneous`, `readings`, and both `reading` wrappers take `max_age: timedelta | None`.
When set, a reading older than that is dropped with a `UserWarning` naming the parameter,
its actual age, and the threshold it failed.

The default is `None` — no filtering — which keeps the functions faithful to the endpoint
and backward compatible. The docstrings carry the live example above and say plainly that
this default is rarely what a caller wants. Making it opt-out rather than opt-in is a
judgement call left to the author, since any non-`None` default silently drops data for
existing callers.

### Verification

Live, 01646500:

```
no max_age (endpoint-faithful):
  00010 Q(24.3 °C (approved))         2019-10-01  age=2510d
  00060 Q(3010 ft³/s (provisional))   2026-08-15  age=0d
  00065 Q(3.03 ft (provisional))      2026-08-15  age=0d
  00095 Q(406 µS/cm (approved))       2019-10-01  age=2510d
  63680 Q(6.2 FNU (approved))         2019-05-27  age=2637d

max_age=timedelta(hours=6):
  00060 Q(3010 ft³/s (provisional))   2026-08-15  age=0d
  00065 Q(3.03 ft (provisional))      2026-08-15  age=0d
  WARN: dropping USGS parameter 00010 ... 2510 days old (2019-10-01)
  WARN: dropping USGS parameter 00095 ... 2510 days old (2019-10-01)
  WARN: dropping USGS parameter 63680 ... 2637 days old (2019-05-27)
```

### Tests

- `tests/test_usgs.py::test_a_reading_knows_how_old_it_is`
- `tests/test_usgs.py::test_a_stale_reading_is_dropped_when_a_max_age_is_given` — builds
  the real-world shape (stale discharge beside a current stage) and asserts the current
  one survives.
- `tests/test_usgs.py::test_max_age_defaults_to_returning_whatever_the_service_holds`
- `tests/test_ea.py::test_a_stale_reading_is_dropped_when_a_max_age_is_given`

The `_payload_dated` helper restamps both series, because the recorded fixture is itself
a day old and a staleness test cannot lean on that.

**Suite: 211 passed, 2 deselected.**

### Remaining gap

`max_age` guards the retrieval boundary. It does not put the observation time on the `Q`,
so a stale value that has already been retrieved is still indistinguishable downstream —
the ledger records magnitude, unit, datum, and quality, but not when the measurement was
taken. Carrying a timestamp on `Q` would let `Spec` declare a freshness requirement the
same way it declares a quality floor. That is a larger change to a frozen dataclass used
everywhere, so it is noted rather than made here.

---

## Fix 9 — the pack refused to guess a datum offset, then guessed the datum

**Severity:** medium-high. The sharpest internal contradiction in the codebase.

**Where:** `src/quantity_guard/packs/usgs.py`, `site`.

### Defect

```python
altitude_datum = field.get("alt_datum_cd", "").strip() or "NAVD88"
```

When a site record stated no altitude datum, one was assumed. The gage offset was then
registered *against that assumption*, so every subsequent stage-to-elevation conversion
inherited it silently.

```
site record with alt_datum_cd blank -> gage_datum = Q(0 ft (NAVD88))
```

`DatumConversionUnavailable` tells the caller that a conversion *"depends on location and
will not be guessed"*. Guessing which datum a published altitude is measured from is the
same error one step earlier, and worse: it is unobservable. Many older NWIS sites are on
NGVD29, and the NAVD88–NGVD29 difference is about the size of the freeboard error the
whole datum subsystem exists to prevent.

A second defect in the same block, the same shape as Fix 5: registration of `GAGE:<n>`
was nested inside `if altitude:`, so a site publishing no altitude left the name
unregistered and every stage reading from it raised `DatumMismatch`.

Third, `alt_acy_va` was read from the record and discarded. It is commonly 0.01 ft on a
modern survey and 10 or 20 ft on an older one, which bounds how far any elevation derived
through the gage offset can be trusted.

### Change

- No default. `altitude_datum` is used only when the record states it; otherwise
  `gage_datum` is `None`, no offset is registered, and a `UserWarning` explains that
  stages will be labelled but not convertible.
- `GAGE:<n>` is registered whenever `register` is set, independent of the altitude, so
  stage readings stay labelled with the station frame either way.
- `Site.gage_datum_accuracy` carries `alt_acy_va` as a `Q` in feet.

### Verification

```
normal (NAVD88 published)  gage_datum=Q(0 ft (NAVD88))   acc=Q(0.01 ft)
alt_datum_cd BLANK         gage_datum=None               acc=Q(0.01 ft)
   WARN: USGS site ... publishes an altitude of 0.00 ft but no alt_datum_cd ...
NGVD29 published           gage_datum=Q(0 ft (NGVD29))   acc=Q(0.01 ft)
no altitude at all         gage_datum=None               acc=Q(0.01 ft)
```

### Tests

- `test_an_unstated_altitude_datum_is_not_assumed`
- `test_a_stated_altitude_datum_is_read_not_defaulted`
- `test_a_site_without_an_altitude_still_registers_its_own_datum`
- `test_the_published_altitude_accuracy_is_carried`

plus a `_site_rdb(**overrides)` helper for building site-record variants.

**Suite: 215 passed, 2 deselected.**

---

## Fix 10 — the missing-value sentinel was matched as a string

**Severity:** medium. A missing measurement entered the ledger as a real one.

**Where:** `src/quantity_guard/packs/usgs.py`, `instantaneous`.

### Defect

```python
if point["value"] in ("", "-999999"):
```

One literal spelling. Anything else passed through as a measurement:

```
value='-999999'    -> filtered      correct
value='-999999.0'  -> Q(-999999 ft³/s (provisional))
value='-999999.00' -> Q(-999999 ft³/s (provisional))
```

A discharge of −999,999 ft³/s is dimensionally valid, carries a genuine provisional flag,
enters the ledger, and satisfies every guard in the library. Meanwhile the service
publishes its own sentinel per variable — `"noDataValue": -999999.0` — in the same
payload, and the pack ignored it.

### Change

Added `_is_missing(raw, no_data)`, which compares **numerically** against the sentinel the
service states for that variable, falling back to `DEFAULT_NO_DATA` when a series omits
it. Non-numeric text is treated as missing too, since NWIS writes an empty string, and
occasionally other placeholder text, for a gap in the record.

### Verification

```
value='-999999'    -> dropped        value=''       -> dropped
value='-999999.0'  -> dropped        value='   '    -> dropped
value='-999999.00' -> dropped        value='Ice'    -> dropped
value='-9.99999E5' -> dropped        value='234000' -> KEPT  Q(234000 ft³/s)

noDataValue=-8888, value=-8888.0 -> dropped   (service-declared sentinel honoured)
```

### Tests

- `test_every_spelling_of_the_missing_value_sentinel_is_dropped` — 7 cases.
- `test_a_real_measurement_is_not_mistaken_for_the_sentinel` — guards against
  over-correction.
- `test_the_sentinel_is_read_from_the_variable_not_hard_coded`

**Suite: 224 passed, 2 deselected.**

---

## Fix 11 — the audit could not see a sign inversion

**Severity:** high, and the most consequential audit gap in this domain.

**Where:** `src/quantity_guard/provenance.py`, `_close_either_sign` / `_judge` /
`AnswerAudit`.

### Defect

Magnitudes were matched with the sign discarded, so a freeboard of −4.06 ft — the levee
overtopped by four feet — reported as positive read as a clean provenance match:

```
tool returned: Q(-4.06 ft)
answer: "The levee has 4.06 ft of freeboard remaining; no action needed."
audit:  ok=True
        [ok] 4.06 ft  from freeboard.return
```

The sign-insensitivity was deliberate and had a good reason: a tool returns a datum
offset of −0.44 ft and the model writes "subtract 0.44 ft", where the sign has moved into
the prose and the figure is still the retrieved one. Fixing that removed a real class of
false positive. But collapsing the two cases meant the audit could not distinguish four
feet of margin from four feet of overtopping, and it flags a restatement of 1350 Ml as
1.35 Ml — a reporting error with no safety consequence — while passing this.

### Change

The comparison now reports *how* it matched rather than only *that* it did.
`_sign_of_match` returns `"same"`, `"flipped"`, or `None`; `_close_either_sign` is kept as
a thin wrapper over it.

A flipped match then asks whether the wording accounts for the flip.
`_sign_carried_in_prose` reads a window on **both** sides of the number, because the
direction word lands on either — "subtract 0.44 ft" before, "0.44 ft below NGVD29" and
"4.06 ft above the crest" after — against a vocabulary of direction words
(`subtract`, `below`, `above`, `drop`, `shortfall`, `overtopped`, …).

- flipped, wording accounts for it → `sourced`, detail notes the sign is in the wording.
- flipped, nothing accounts for it → new status **`sign_inverted`**.

`AnswerAudit.sign_inverted` exposes them, `report()` marks them `[SIGN INVERTED]`, and
`ok` now includes them. It is reported apart from `mislabelled` because the failure is
different in kind: the unit is right, the figure really was retrieved, and the condition
described is the opposite one.

Only a *flipped* match consults the prose, so a direction word beside a value whose sign
already agrees ("31.0 ft above NAVD88") is never reached and cannot excuse anything.

### Verification

```
THE HAZARD (tool returned -4.06 ft):
  ok=False  The levee has 4.06 ft of freeboard remaining; no action needed.  -> sign_inverted
  ok=False  Freeboard is 4.06 ft.                                            -> sign_inverted

CORRECT restatements of the same negative value:
  ok=True   The levee is overtopped by 4.06 ft.                              -> sourced
  ok=True   The water surface is 4.06 ft above the crest.                    -> sourced
  ok=True   Subtract 0.44 ft to convert to NGVD29.                           -> sourced
  ok=True   The gage datum sits 0.44 ft below NGVD29.                        -> sourced
  ok=True   A shortfall of 4.06 ft.                                          -> sourced
  ok=True   Freeboard is -4.06 ft.                                           -> sourced
```

The false positive the sign-insensitivity was added to fix stays fixed.

### Tests

- `test_a_negative_value_restated_with_the_sign_in_the_wording_is_sourced` — 5 cases,
  extending the original single-case test.
- `test_a_flipped_sign_with_nothing_to_account_for_it_is_reported`
- `test_a_directional_word_cannot_excuse_a_value_whose_sign_already_agrees`

**Suite: 232 passed, 2 deselected.**

---

## Fix 12 — audit blind spots: large bare numbers, thin literals, and the domain vocabulary

**Severity:** medium. Three independent gaps, two of which flagged *correct* answers.

**Where:** `src/quantity_guard/provenance.py` (`_WORD`, `_parse_unit`, `_is_ignorable`,
`_close`), `src/quantity_guard/registry.py`.

### (a) The domain's own unit names were unparseable

```
"35.4 cumecs"      -> unsourced      <- the correct converted answer
"1250 cusecs"      -> unit dropped
"1250 CFS"         -> unit dropped
"150000 acre-feet" -> parsed as "acre", then unsourced
```

`cumec` and `cusec` are standard Commonwealth usage — the vocabulary the `--suite uk`
work is about — and `acre-ft` is the standard US storage unit. All produced false
positives on correct answers, which is the failure the README reports as driven to 0%.

**Change.** `cumec` and `cusec` defined in the registry (which fixes `Q.parse` too, not
just the audit). `_WORD` accepts a hyphenated compound as one word, and `_parse_unit`
maps the hyphen to a product. An all-caps token falls back to lowercase only when the
written form fails to parse, so a genuine capital such as the mega prefix is never
overridden.

### (b) Any bare integer of five digits or more was discarded

```
"The reservoir released 150000."  ->  (no numeric claims found)   ok=True
```

The rule existed to skip site numbers, but in this domain five- and six-digit integers
are discharges, reservoir releases, and populations at risk — exactly the fabrications
the audit exists to catch.

**Change.** Threshold raised to eight digits, which still covers USGS site numbers, and
a number introduced by `station` / `site` / `gage` / `gauge` / `no.` / `#` is treated as
an identifier at any length. Zero-padding still marks an identifier as before.

### (c) A single significant figure matched almost anything

```
ledger 1250 cfs,  answer "1 kcfs"  ->  sourced
```

The rounding rule accepts a restatement at the precision written. At zero decimals with a
one-digit value that turns a 0.5% tolerance into roughly ±40%.

**Change.** The rounding fallback is skipped when the literal has zero decimals and an
absolute value below 10. The documented cases are unaffected — 17.1 written as "17" and
14.23 written as "14.2" both still match — because both carry at least two figures.

### Verification

```
(a)  1250 cfs / CFS / cusecs / cusec / ft3/s / ft^3/s  -> sourced
     35.4 cumecs                                       -> sourced   (was unsourced)
     150000 acre-ft / acre-feet                         -> sourced
     1250 Ml/d                                          -> unit_mislabelled  (unchanged)
     999 cfs                                            -> unsourced         (unchanged)

(b)  "The reservoir released 150000."   -> sourced      (was invisible)
     "The reservoir released 999999."   -> UNSOURCED    (was invisible)
     "At station 07374000 ..."          -> ignored
     "Site 12345678 ..."                -> ignored
     "Gage 4155 is offline."            -> ignored
     "surveyed in 1974" / "12 properties" -> ignored

(c)  ledger 1250 -> "1 kcfs"     unsourced   (was sourced)
     ledger 17.1 -> "17 ft"      sourced     (documented case, unchanged)
     ledger 14.23 -> "14.2 ft"   sourced     (documented case, unchanged)
```

### Tests

Six new tests in `tests/test_provenance.py` covering each change plus its
over-correction guard, including `test_identifiers_and_counts_are_still_left_alone`.

**Suite: 247 passed, 2 deselected.**

### Known limitation left in place

A multi-word unit written with spaces rather than a hyphen ("150000 acre feet") still
tokenises as `acre`. Handling it needs a multi-token unit grammar rather than the
one-or-two-word pattern, which is a larger change than this finding warrants.

The derivation false-accept rate on small integers is unchanged: with a three-output
ledger of 3, 5 and 11 ft, the fabricated values 8, 14 and 2 are all still `derived`.
Sums and differences of small integers collide densely, and tightening it would reject
legitimate derivations. The README's measured rate is for randomly chosen numbers and
understates the rate for the small integers that actually appear in answers — worth
saying explicitly in the Limitations section.

---

## Fix 13 — the proxy held one ledger for the life of the process

**Severity:** high. A correctness bug and a confidentiality one in the same line.

**Where:** `src/quantity_guard/proxy.py` (`GuardedProxy`, `_dispatch`, `serve_stdio`,
`main`), `src/quantity_guard/provenance.py` (`Session.max_entries`).

### Defect

`main()` wrapped the entire `serve_stdio` loop in a single `open_session()`. A stdio
server outlives any one conversation, so every conversation shared a ledger:

```
-- conversation 1 --
read_discharge -> 1250 cfs
runoff_depth(discharge=1250)  -> carry-over caught          correct

-- conversation 2, same process, no read_discharge call at all --
runoff_depth(discharge=1250)  -> carry-over caught          WRONG
   "...but read_discharge.return returned 1250 cfs and no conversion was applied"
```

Three problems in one line: a false positive on a legitimate call; an **error message
quoting another conversation's tool output back to this caller**; and an unbounded ledger
(503 entries after 503 calls, never trimmed).

Three smaller defects alongside it:

- `self.ledger.calls` was never incremented, so `enforcement_report()` always said
  *"No calls would have been blocked across 0 tool calls."*
- `_validate` raised a bare `GuardViolation`, so a carry-over reported as
  `[guard_violation]` through the proxy and `[unconverted_carry_over]` through the
  decorator. Anything keyed on `code` broke depending on the path.
- An unsupported method returned `-32603` (internal error) where JSON-RPC specifies
  `-32601` (method not found), so a client probing for `resources/list` saw a server
  fault.

And one gap: `_enrich` returned early when a tool declared no parameters, so **return
units were never advertised**. In the demo, `read_discharge` and `read_drainage_area`
shipped with no unit in the schema at all — while the README's own argument is that a
model reading `cfs` has to already know the expansion, and the schema is where it would
have learned it.

### Change

- `Session` gained `max_entries`, trimming oldest-first. `None` keeps everything, which
  is right for a scoped agent run where the manifest must be complete.
- `GuardedProxy.begin_session()` starts a fresh ledger, called from `_dispatch` on
  `initialize` — the point at which MCP says a new client session begins.
- `main()` builds a `Session(max_entries=LEDGER_LIMIT)` (512) instead of an unbounded one.
- `call_tool` increments `calls` for guarded tools only, so the denominator in
  `enforcement_report()` means something.
- The carry-over raise uses `UnconvertedCarryOver`.
- `MethodNotFound` is raised and mapped to `-32601`, leaving `-32603` for real faults.
- `_enrich` emits `outputSchema` from the return declaration and appends the unit to the
  tool description, keeping whatever the upstream already said.

### Verification

Driving the real proxy over stdio against `demo/usgs_server.py`:

```
tools/list read_discharge      return x-unit='cfs'
    description: Latest observed discharge at a streamgage, in cfs. Returns a value in cfs.
tools/list read_drainage_area  return x-unit='km**2'
tools/list runoff_depth        return x-unit='mm/day'

id 3: {"value": 1250.0, "unit": "cfs"}
id 4: [unconverted_carry_over] for `discharge` received the bare number 1250 ...
id 5: initialize            <- new client session
id 6: {"value": 3.724..., "unit": "mm / d"}   <- same call, now accepted
id 7: {"code": -32601, "message": "method not found: resources/list"}
```

id 4 and id 6 are the same call; only the intervening `initialize` differs.

### Tests

Seven new tests in `tests/test_proxy.py`:
`test_carry_over_reports_the_same_code_as_the_decorator`,
`test_a_new_client_session_does_not_inherit_the_previous_one_s_ledger`,
`test_the_ledger_is_bounded_when_a_limit_is_set`,
`test_guarded_calls_are_counted`,
`test_a_declared_return_unit_is_advertised_to_the_model`,
`test_an_unsupported_method_is_method_not_found_not_a_server_fault`,
`test_initialize_over_the_transport_resets_the_ledger`.

**Suite: 254 passed, 2 deselected.**

### Note for the README

*"It reads the server's tool list, merges in declarations from an annotation file, and
re-advertises the tools with units in the schema"* was true of parameters only. It is now
true of returns as well.

---

## Fix 14 — type-level defects

**Severity:** medium. Six independent problems in the core type and declaration layer.

### (a) `Q.parse` rejected the spellings the services publish

```
"1250 ft3/s"  -> UnitParseError: 'ft3' is not defined
"1250 m3/s"   -> UnitParseError: 'm3' is not defined
"1250 acre-ft"-> UnitParseError
```

`ft3/s` is verbatim what `variable.unit.unitCode` contains in every NWIS response, and
`m3/s` is how models write units in prose. The answer audit already normalised these; the
tool boundary did not. The same string was therefore valid in an answer and rejected as
an argument — penalising a model for stating its unit in the form the service published.

**Change.** The normalisation moved to `registry.normalize_unit_text`, shared by
`Q.parse`, `Q.__post_init__`, and the audit's `_parse_unit`, so the two entry points can
no longer disagree. Scientific notation is protected: `"1e3 cfs"` still parses as 1000.

### (b) Temperature arithmetic raised a raw pint exception

```
Q(21,"degC") + Q(1,"degC")  -> OffsetUnitCalculusError   (raw pint)
Q(21,"degC") * 2            -> OffsetUnitCalculusError   (raw pint)
```

Only `pint.DimensionalityError` was caught. pint's refusal is correct — a degree Celsius
is a point on a scale, not an amount — but raising it raw means no `repair()` text and no
tool-error path, and the model gets a pint URL. Water temperature is a first-class
hydrology variable that the USGS pack maps straight onto `degC`, so this is reachable
from ordinary retrieval.

**Change.** `_offset_unit_error` wraps it as a `DimensionalityError` explaining the
distinction and naming the two fixes (difference to an interval, or convert to kelvin).
Applied in `__add__`, `__sub__`, and `_scale`. Differencing two temperatures still works.

### (c) NaN and infinity passed every check

```
Q.parse("nan cfs")                    -> Q(nan cfs)
json.dumps(Q(nan,"m**3/s").as_dict()) -> {"value": NaN, ...}    not valid JSON
```

A NaN fails every comparison silently, so it surfaced from the audit as *unsourced*
rather than as invalid.

**Change.** A non-finite **scalar** is refused — it has no gap to represent, so it is a
failed computation or an escaped sentinel. A **series** keeps its NaNs, because that is
how a time series marks a missing sample, and `as_dict` writes them as `null` so the
payload stays valid JSON. `__format__` now reports gaps (`[3 values, 1 to 3, 1 missing]`)
instead of collapsing the range to `nan to nan`.

### (d) A `returns` mapping broke on natural key names

```python
returns={"stage": {"unit": "ft"}, "quality": {...}}
-> TypeError: Spec.__init__() got an unexpected keyword argument 'stage'
```

Membership was decided by *intersection* with Spec's field names, so one colliding key
made the whole declaration read as a single Spec. `quality` and `datum` are among the most
natural result names for a hydrology tool.

**Change.** Subset is the correct test — a single Spec's keys are all Spec fields by
definition. `sourced`, previously missing from the set, was added.

### (e) `VA` and `var` were silent aliases of the watt

```
Q(100,"VA").to("var") -> Q(100 var)
Q(100,"VA").to("W")   -> Q(100 W)
```

Both were defined as `volt * ampere`, directly contradicting the comment above them
(*"the distinction has to survive a round trip"*). Real, apparent, and reactive power are
dimensionally identical and not interchangeable: converting between them needs a power
factor, which is a property of the circuit, not of the units. The `--suite grid` result
rests on this.

**Change.** `VA = [apparent_power]` and `var = [reactive_power]` — separate dimensions, so
all three refuse to interconvert. SI prefixes still work (`MVA`, `kvar`).

### (f) `mgd` silently meant US gallons

`1 mgd` resolved to 3.785 Ml/d. In UK water-resources practice "mgd" means million
*imperial* gallons per day, 4.546 Ml/d — a 20% error in a licensed abstraction, in a
library that ships a UK pack and a UK benchmark suite about Ml/d.

**Change.** `us_mgd` (aliased `mgd`, matching USGS, which is where the abbreviation is
actually published) and `imperial_mgd` are named apart. Neither spelling stands for both.

### Verification

```
(a) '1250 ft3/s' -> Q(1250 ft³/s)      '150000 acre-ft' -> Q(150000 acre·ft)
    '1250 m3/s'  -> Q(1250 m³/s)       '1e3 cfs'        -> Q(1000 cfs)
    '35.4 cumecs'-> Q(35.4 cumec)      '1250'           -> BLOCKED (carries no unit)

(b) degC - degC -> Q(3 Δ°C)            degC + degC -> BLOCKED DimensionalityError
    degC -> degF -> Q(69.8 °F)         degC * 2    -> BLOCKED DimensionalityError
    K * 2 [legit] -> Q(588 K)

(c) Q(nan) / Q(inf) / Q.parse("nan cfs") -> BLOCKED UnitParseError
    series [1, nan, 3] -> Q([3 values, 1 to 3, 1 missing] ft)
                       -> {"value": [1.0, null, 3.0], "unit": "ft"}

(e) VA->W / VA->var / var->W -> BLOCKED     MVA->VA, kvar->var, MW->W -> converted

(f) 1 us_mgd -> 3.78541 Ml/d      1 imperial_mgd -> 4.54609 Ml/d
```

### Tests

Ten new tests in `tests/test_quantity.py` and one parametrised set in
`tests/test_tool.py`, each with its over-correction guard.

**Suite: 281 passed, 2 deselected.**

### Left as it was

`Q(series) > Q(scalar)` returns a numpy array, so `bool()` on it raises. This is normal
numpy semantics and useful, but it contradicts the README's *"declarations behave
identically for both"*. Changing it would break elementwise comparison for anyone using
it; the README sentence is the thing to amend.

---

## Summary

| # | Finding | Status |
|---|---|---|
| 1 | `elevation * 1` stripped the vertical datum | fixed |
| 2 | `enforcement="warn"` rejected on the return path | fixed |
| 3 | `require_explicit_unit` enforced on 1 of 4 input shapes | fixed |
| 4 | Quality gate vacuous; NWIS condition codes discarded | fixed |
| 5 | `ea.reading()` raised on ~45% of real EA stations | fixed |
| 5b | Station references interpolated into URLs unescaped | fixed |
| 6 | EA quality read from a non-existent field | fixed |
| 7 | `deg C` unmapped; one bad unit aborted the station read | fixed |
| 8 | No staleness concept; 2019 readings returned as "latest" | fixed (opt-in) |
| 9 | `alt_datum_cd` defaulted to NAVD88 | fixed |
| 10 | `-999999` sentinel matched as a string | fixed |
| 11 | Sign inversion passed the audit as `sourced` | fixed |
| 12 | Audit: large bare numbers, thin literals, domain vocabulary | fixed |
| 13 | Proxy held one ledger for the process lifetime | fixed |
| 14 | Type-level: parse, degC, NaN, returns keys, VA/var, mgd | fixed |

**Suite: 141 → 281 passing.** The demo and the stdio proxy were re-run end to end after
the last change and behave as documented.

### Behaviour changes needing a release note

1. **A declared `quality` floor now refuses unflagged record** (Fix 4). Callers already
   declaring a floor will start seeing `QualityViolation` where values carry no qualifier.
2. **`VA` and `var` no longer convert to `W` or to each other** (Fix 14e). Correct, but it
   will break any code relying on the old aliasing.
3. **`mgd` is now `us_mgd`** (Fix 14f). The spelling still resolves; the ambiguity does not.
4. **A non-finite scalar magnitude is refused** (Fix 14c).
5. **Scaling a datum-bearing value is refused** (Fix 1), as is `delta - elevation`.

### Still open, by choice

- `max_age` defaults to `None`, so staleness filtering is opt-in (Fix 8).
- `Q` carries no observation time, so a stale value already in the ledger is still
  indistinguishable downstream (Fix 8).
- Derivation still false-accepts small integers; the README's measured rate is for random
  numbers and understates the small-integer case (Fix 12).
- Multi-word units written with spaces ("150000 acre feet") still tokenise on the first
  word (Fix 12).
- The `tz` check silently rewrites the timestamp into the declared zone, which the
  Limitations section does not mention.
- `datetime.fromisoformat` handles a `Z` suffix on 3.11+ but not on 3.10, which
  `pyproject.toml` declares as supported; `_lenient` strips it manually and `_coerce_time`
  does not.

---

## README

Rewritten after the fixes. Every behaviour change above is recorded under a `## Changes`
section, and every numeric claim was recomputed from `bench/*.jsonl` rather than carried
forward. Six claims did not survive that check:

| claim as written | what the data shows |
|---|---|
| "eleven models from **seven** families" | eight |
| carry-over table "Llama 3.3 70B **17/17**" | 14/15 by call log |
| "Under enforcement the error **reaches the tool on no run** of any model" | the model still *sends* it at similar rates; the guard rejects the call |
| "Counted on **what reached the tool**" | `undetected`/`silent_error` are computed from the final answer plus violations, not from the call log |
| grid suite: "every model converted correctly on **all 192 runs**" | true at baseline (48/48); `schema_only` had 4 silent errors and 41/48 correct |
| accuracy "**75-83%** at baseline to **94-100%** enforced" | 60–86% → 98–100%, for the seven protocol-following models |

The third and fourth are the substantive ones. The README described a metric — what the
model passed into the tool — that the harness does not compute; `undetected` and
`silent_error` are both properties of the final answer. The tables are now computed from
`calls_log`, which does record what was sent, and the two measurements are defined
separately in the Method section so they cannot be read as interchangeable.

Recomputing that way also changes the story in a way worth keeping: enforcement does not
stop the model sending the wrong magnitude, it stops the wrong magnitude having
consequences. On the runoff task that is 46 silent errors at baseline against 3 guarded —
not zero, and the README now says so.

The grid finding was inverted by the check. `schema_only` underperformed `baseline` there
(41/48 against 48/48, four silent errors from one model on one task), which is the only
cell where declaring units without enforcing them made things worse. It is now reported
with that caveat rather than averaged away.

Scientific presentation was tightened throughout: conditions and metrics defined before
results; denominators, per-model dropout (0%–27.8%) and total exclusions (15.1%) stated;
the opacity account labelled as a hypothesis with its contradicting case; and a
Limitations section that names the design's resolution limit, the confound between model
identity and provider reliability, and each check's stated scope.

Two prior claims were also qualified rather than removed: the audit's measured
false-positive rate predates the `sign_inverted` check and the tokenisation changes, so it
is flagged as re-measurable rather than carried forward; and the derivation false-accept
rates are noted as applying to randomly chosen numbers, understating the small-integer case.

All code examples in the README were executed against the library and pass.
