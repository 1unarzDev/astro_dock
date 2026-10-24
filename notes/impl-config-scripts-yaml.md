# Static Linux script YAML implementation

Base: `eb9c608301071f26a86d3d3aad84b5aa11d14f40`; isolated branch
`impl/config-scripts-yaml`. SY01–SY05 are implemented for opt-in Linux/Jazzy
static off-boat startup only. No running container or DDS delivery was tested.

Implementation commits (added + deleted lines):

- `980dbfe`: installed pure YAML compiler/defaults and validation tests (134).
- `d0f8c1e`: immutable generation, source/artifact hashes, projection verification (145).
- `919165d`: Linux Compose null placeholder compatibility correction (3).
- `fd4e688`: actual prebuild, installed host helper and image packaging seam (108).
- `ab26618`: actual Bash/RViz frozen selection consumers and inert shell tests (119).
- `c4fd7f4`: installed explicit catalog provider and wheel/catalog tests (247).
- `635fc22`: emptied-selection refusal and additional boundary tests (25).

The original Linux adapter fails real Docker Compose 5.6.0 validation with
`services.lidar-relay must be a mapping`. Its `lidar-relay:` value was null,
with no image, command or behavior. The separate compatibility commit removes
only that invalid placeholder; this also allows legacy unselected Linux Compose
to validate. Existing dev/SITL mappings remain. Owner review should assess this
small deliberate legacy-path correction.

Focused validation: seven tests across `tests/test_run_*.py` passed using normal
installed distributions in `/tmp/astro-run-tools-test`, from unrelated cwd where
applicable. Actual `docker compose ... config --format json` renders only;
no daemon, build, pull, start or physical/network container operation was run.
Actual Bash runs in a bwrap filesystem sandbox with inert ROS setup, ros2,
python3 scan and rviz stubs. Noninteractive BASH_ENV, interactive source, direct
exec environment and managed RViz match one generated Compose environment.
Corruption/conflicting/empty selection refuses before overlays and child output.
Malformed/unsupported selections retain the prior generated adapter.
Normal wheels are built/installed outside the checkout; missing core refuses,
and installed catalog pointers/hashes resolve. No PYTHONPATH/source fallback.
Host bootstrap and packaging tests explicitly disable package indexes and pip
version checks. Initial local pip install checks used no dependencies/build
isolation; their automatic pip update-check traffic was not instrumented, so
no blanket network-isolation claim is made for those earlier commands.

`astro_run_config.catalog_bundle()` is the explicit installed read-only provider,
with owner `astro-run-config`. It returns existing core `DescriptorBundle` rows
and pinned resource bytes, ready to pass alongside hardware/nav bundles to the
existing testing `assemble(...)` explicit input. This isolated source pin has
no testing package/assembly caller. Cross-repo assembly and central production
UI/CLI linkage remain a dependent assembly packet; standalone provider coverage
does not establish central registry integration. No plugin engine was added.

Limits: image Dockerfile wiring was inspected, not built; managed images must
be rebuilt with the existing build Compose config before selection. No endpoint
or delivery verification, boat networking, DDS interface pinning, daemon control,
host boot/network changes, full suite, merge, integration branch or push.

## Bounded review repairs (SYR01–SYR04)

- `ee880f0`: validate baseline separately from effective session values; freeze
  `ROS_DISTRO=jazzy` into every selected environment; reject dollar-containing
  data paths before rendering until downstream Compose escaping has a proof.
  Quote/space paths remain supported. Focused compiler tests: 3 passed; frozen
  projection test: 1 passed, including retained prior output on dollar refusal.
- `e917f8e`: clear overlay-introduced builtin/Cyclone/localhost/subnet/address and
  domain-alias controls, then restore frozen values after every installed setup,
  secondary-workspace colcon setup and managed RViz boundary. Actual inert Bash
  overlay test passed with each control injected, wrong-distro refusal before
  overlay sentinel changes, and quote/space/backtick payload kept literal with
  no payload file created. Direct environment and real Compose config match.

These repairs do not claim dollar-path runtime corruption: review established
only a missing acceptance proof for Compose serialization. The temporary policy
fails closed before generation instead of requiring any container operation.
Focused prebuild/installed wheel regressions are rerun for the changed projection;
no full suite, container create/start, ROS nodes, daemon/network controls, main
or integration branch edits, merge or push are part of this repair.
