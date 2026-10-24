# Package descriptor interface (schema 1)

`mhseals_config.catalog.DescriptorBundle.read(data, resources, registry,
consumer_versions)` accepts an already parsed mapping. `read_yaml` accepts YAML
text with duplicate/merge/recursive-alias checks inherited from the core parser.
Both return immutable metadata and rows. This interface imports no domain code,
opens no files, invokes no registry callable, and performs no configuration
precedence or lowering. Domain compilers remain authoritative.

Supply `resources` as `{source_id: immutable_bytes}` obtained explicitly from the
installed package. The descriptor's `default_sources` entries each contain
`id`, package-relative `resource`, and `sha256` of those bytes. No default values
belong in descriptors. Supply `registry` as explicit `{reference_id: callable}`
bindings owned by the application/domain. There are no import strings or YAML
expressions. `consumer_versions` maps installed consumer IDs to exact build IDs.
An incompatible installed version refuses; an absent version produces a blocker.

The top level has exactly `schema_version: 1`, `owner_package`, `revision`,
`consumer_versions`, `default_sources`, and `leaves`. Each leaf requires
`canonical_key` and finite `aliases`. Optional incomplete sections create explicit
blockers. All supplied section fields must be known; supported metadata should
supply every field below, using `unknown` or `unsupported` where appropriate:

- `default_ref`: `source_id`, JSON `yaml_pointer`, `path`, positive source `line`,
  `source_hash`; alternatively `invariant_ref`, bound in the explicit registry.
- `value`: `type` (boolean/integer/number/string/sequence/mapping), `dimensions`
  (exact sequence lengths by depth), `units` (or `unitless`), `constraint_ref`,
  `validator_ref`, `inputs`.
- `authoring`: `destination`, `pointer`, `layers`, `absence`, `null`, `reset`,
  `scopes`, `controlling_keys`. Layers are package/deployment/profile/operator/
  session/patch/trial/derived. Scopes are baseline/session/trial. A derived leaf
  with controlling keys cannot declare baseline scope. Null defaults need
  `null: allowed` or `null: follows-controlling-key` to avoid a blocker.
- `consumers`: list of `id`, `version`, `parameters`, `compiler_ref`, `inputs`,
  `outputs`, `selection_ref`, `dependencies`, `candidate_tier` (integer 1–5 or
  `unsupported`), `owner_group`.
- `safety`: `policy_ref`, `armed_ref`, `goal_ref`, `procedure_ref`, `hold_ref`,
  `confirmation_ref`, `compensation_ref`, `state_ref`.
- `evidence`: list of `recipe_ref`, `consumer`, `version`, `required_type`.
- `coverage`: list of `source`, `consumer`, `status`, `revision`, `path`, `line`,
  `consume_site`. Status is discovered/traced/startup-supported/live-supported/
  behavior-verified/unsupported/fixed-invariant. These are declarations, never
  achieved observations.

The initial schema supports finite explicit leaves only. Enumerate every family
descendant with its own type and consumer contract; `bundle.field(key)` performs
exact canonical lookup. Cross-bundle identity checks and required missing-package
reporting belong to the subsequent pure assembly slice.

Each row exposes `descriptor`, referenced `default`, `blockers`, and
`writable == False`. Missing metadata, unresolved references and unknown versions
are read-only inventory. Every row additionally carries
`runtime-authority-not-provided`; complete descriptors, declared tiers, and recipe
names never grant writes or assert achieved runtime evidence. Actual requested,
effective, origin, and B/S/T information must come from existing compiler/runtime
results rather than this descriptor API. No callable is run during inventory;
validators and compilers require their documented exact input closure at the
domain boundary. Safety predicates and state-preservation evidence still require
runtime adapters.

Tests use a small hardware timeout/navigation frequency fixture, not installed
package descriptors. Hardware correction/output and MPPI/shim/smoother descriptors
are separate package-owned follow-ups.
