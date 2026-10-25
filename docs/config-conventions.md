# Configuration conventions

Naming, layout, precedence and tiers only. Rules carry IDs (`R1`..`R4`) so later
packets can cite them. Layer semantics are defined by the configuration spec
(section 4, `specs/config_unification.md`); this page only names them.

## R1 Layout
Package defaults live in `config/`; host or boat specific files in `host/`;
vendored third party defaults in `vendor_*`. Installed config is listed
explicitly in `setup.py`/`CMakeLists.txt`, never by glob. ROS node parameter
files use the `/**: ros__parameters` wrapper; domain YAML (hardware, health,
keymap, history, catalogs) is a plain mapping. A file never mixes the two.

## R2 Names and units
Keys are `snake_case`. A quantity names its unit from this closed list, in the
catalog row's `unit` field (a key suffix only for a bare quantity, e.g.
`timeout_s`):

`s, Hz, m, m/s, rad, rad/s, rad/s^2, m/s^2, count, bool, enum, frame, topic, path, 1`

(`1` is dimensionless.) A list or map of mixed quantities joins its units with commas
(`m/s,m/s,rad/s`); `ID:Hz` marks an id-to-rate map. Nav2, robot_localization, ZED and MAVROS keys and every
`vendor_*` file keep their upstream spelling (e.g. `FollowPath.TwirlingCritic`);
their catalog rows still carry a unit from the list.

## R3 Layers and precedence
Lowest first: `package`, `deployment` (boat or site), `profile`, `operator`,
`session`, `patch`/`trial` (the catalog accepts both spellings), `derived`
(deterministic derivations and non-overridable safety constraints).

Environment variables select a path; they are not a value layer. Path
precedence: explicit argument, then `MHSEALS_<SURFACE>_CONFIG` (surface
upper-cased), then the operator file
`${XDG_CONFIG_HOME:-~/.config}/mhseals/<surface>.yaml`.

## R4 Tiers
A tier is what applying a change costs (spec section 7). The owner's catalog
stores it as `candidate_tier`; `reload.yaml` and `nav2_classes.yaml` are derived
views and may not disagree with it.

| tier | name | meaning |
|---|---|---|
| 1 | live | atomic apply in the running node |
| 2 | reconfigure | lifecycle RESET/configure/STARTUP of the real lifecycle-manager group |
| 3 | restart | the affected process or launch group is restarted |
| 4 | rebuild | C++/interface dependency closure rebuilt into a versioned isolated prefix, then restarted |
| 5 | reboot | hardware or FCU reboot, queued for a safe owner window |
| - | new-session | environment or Compose values; regenerated, every process restarted |
| - | next-use | read at the next use; nothing running changes |

Legacy names: `reset` (`nav2_params.CLASSES`) is reconfigure; `child`
(`reload.py`) is restart; `next` (`reload.py`) is next-use; `disarmed`
(`reload.py`) is a gating condition, not a tier. Derived views use the legacy
names until A6.

The validator (`catalog.py`) accepts only an integer 1-5 or `unsupported`.
`new-session` and `next-use` are vocabulary only until A6-CORE extends it.
`unsupported` and the derived class `unverified` (a key with no consumer
evidence) are not tiers. A tier is never `live` or supported without an adapter
and a behavioural readback.
