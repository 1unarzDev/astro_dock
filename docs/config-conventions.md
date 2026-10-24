# Configuration conventions

One page. Each rule has an ID (`R1`..`R12`) so that later packets can cite the
rule they implement. The layer order is defined by the configuration spec
(section 4, `specs/config_unification.md`); this page only names it.

## R1 Layout
Package defaults live in `config/`; host or boat specific files in `host/`;
vendored third party defaults in `vendor_*`. Installed config is listed
explicitly in `setup.py`/`CMakeLists.txt`. No install globs, so a stray file can
never become a shipped default.

## R2 Wrappers
ROS node parameter files use the `/**: ros__parameters` wrapper. Domain YAML
(hardware, health, keymap, history, catalogs) is a plain mapping with no
wrapper. A file never mixes the two.

## R3 Names and units
Keys are `snake_case`. A leaf that carries a quantity names its unit from this
closed list and no other spelling. The unit lives in the catalog row's `unit`
field; a key suffix is used only for a bare quantity (`timeout_s`), never a
compound spelling such as `m/s^2` in a key:

`s, Hz, m, m/s, rad, rad/s, m/s^2, count, bool, enum, frame, topic, path, 1`

(`1` is dimensionless.) A new unit needs an edit to this page first.

Upstream exception: Nav2, robot_localization, ZED and MAVROS keys, and every
`vendor_*` file, keep their upstream spelling (for example
`FollowPath.TwirlingCritic`) and are never renamed. Their catalog rows still
carry a unit from the list.

## R4 Canonical keys
Every leaf has one dotted canonical key (`hardware.command_mux.mode_ch`).
Aliases are listed in the owner's catalog and canonicalised within a layer; two
assignments to one canonical leaf in one layer are refused.

## R5 Layers
Layer names are those of spec section 4 and `mhseals_config.catalog._LAYERS`,
lowest first: `package`, `deployment` (boat or site), `profile`, `operator`,
`session` (ephemeral), `patch`/`trial` (layer 6, transaction patch; the catalog
accepts both spellings), `derived` (deterministic derivations and
non-overridable safety constraints). The resolver (`resolver.ORDER`) still
spells `package` as `defaults`; that is a legacy name until it is renamed.
Environment variables select a path, they are not a value layer. Precedence is explicit path, then path-selection
environment, then default path.

## R6 Operator files
Operator overrides live at `${XDG_CONFIG_HOME:-~/.config}/mhseals/<surface>.yaml`.
`MHSEALS_<SURFACE>_CONFIG` (surface upper-cased) overrides the path. An explicit
argument beats both.

## R7 One strict loader
This is the target rule; known violations are tracked as A1/A7 (the hardware
reload and mux still use `safe_load` plus a shallow merge, there is no public
`load_strict` and no per-surface policy declaration yet). Today the strict
parser is the one in `resolver.py`.
All YAML is read through the core strict parser: located errors; duplicate keys,
`<<` merge keys and recursive aliases are refused even when values match. Each
surface declares a policy: `managed` (a refusal records a snapshot) or
`cosmetic`. Under both policies a refused live reload keeps the last good
config. A cosmetic surface may fall back to defaults only at startup or first
load, with a visible notice, never silently.
No `yaml.safe_load` on config inputs.

## R8 Completeness
Every installed leaf and every declared ROS parameter has a catalog row with an
owner, a unit (R3), real consumers and a tier. A row naming a consumer that does
not read the key is a defect; mark it `unconsumed` or `dormant`.

## R9 One tier vocabulary
Tier describes what applying a change costs (spec section 7):

| tier | name | meaning |
|---|---|---|
| 1 | live | atomic apply in the running node; parameters and derived model switch together |
| 2 | reconfigure | lifecycle reconfigure: RESET/configure/STARTUP of the real lifecycle-manager group |
| 3 | restart | the affected process or launch group is restarted |
| 4 | rebuild | the C++/interface dependency closure is rebuilt into a versioned isolated prefix, then restarted |
| 5 | reboot | hardware or FCU reboot, queued for an explicit safe owner window |
| - | new-session | environment or Compose values; regenerated, every process restarted |
| - | next-use | read at the next use, nothing running is changed |

Legacy names in the derived views map as follows. After A6 the views must emit
only the R9 names above; the legacy names are accepted until then.

| legacy name | source | R9 name |
|---|---|---|
| reset | `nav2_params.CLASSES` | reconfigure (2) |
| restart | `nav2_params.CLASSES` | restart (3) |
| child | `reload.py` | restart (3) |
| next | `reload.py` | next-use |
| disarmed | `reload.py` | not a tier: a gating condition on applying |

The tier is stored in the owner's catalog (`candidate_tier`). The core
validator (`catalog.py`) currently accepts only an integer 1-5 or `unsupported`
there. `new-session` and `next-use` need the validator extension that A6-CORE
must land (DAG: A6-CORE depends on A0); until then they are vocabulary only and
a catalog row may not use them. `unsupported` is the catalog value for "no
supported tier yet"; `unverified` is a derived class (for example in
`nav2_classes.yaml`) for a key with no consumer evidence. Neither is a tier.
`reload.yaml` and `nav2_classes.yaml` are derived views and may not disagree
with the catalog. A `candidate_tier` is never `supported` without an adapter and
consumer evidence (a behavioural readback). A key with no such evidence is
`unverified`, never `live`.

## R10 Comments
Comments state the reason, the unit if not in the key, and the owner of any
shared value. They never restate the value. No commented-out settings.

## R11 One owner per shared value
A value used by two systems has one owner row; the other side is a read-only
comparison or a derived value. Example: mux `mode_ch`/`modes` (hardware) against
FCU `MODE_CH`/`MODE1..6` (GPS-FCU owner). A mismatch is reported, not written.

## R12 No hidden writers
Documentation and catalogs do not authorise a writer. Any runtime write path
needs its own owner decision and readback evidence.
