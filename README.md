# astro_dock

A ROS 2 Jazzy container workspace and the `astro` tool for driving robots. It is general: everything about
one team or robot (fleet, network, hosts, devices, tests) lives in a profile set and the team's own repos,
never here.

From the operator laptop's host shell (no ROS there: these run on the profile's `astro.home`, e.g. the boat
computer, and print `(on HOST)`; `--here` runs one here):

```bash
astro up                           # start the robot's stacks, each on its host; waits until they publish
astro rec start lean lake          # record a bag of the "lean" preset, labelled "lake"
astro test still                   # run one test in this terminal (Ctrl-C sends neutral)
astro apply still                  # write its result into the state file
astro mark "wind picked up"        # a line in the day's log, also in the bag
astro rec stop && astro down       # close the bag, stop the stacks
```

`astro help` browses every command (astro's and each package's), `astro help --list` prints them all, and
`astro VERB --help` explains one, with the plain ROS 2 command it runs. `astro docs` prints where everything
lives: the field repo, today's log and bags, and every repo's docs with their GitHub links.

## Setup

```bash
git clone --recurse-submodules https://github.com/1unarzDev/astro_dock.git && cd astro_dock
./setup.linux.sh --dry-run          # preview the stages, then run without --dry-run
.devcontainer/prebuild.sh
devcontainer up --workspace-folder .
devcontainer exec --workspace-folder . bash
```

`setup.linux.sh` asks before each stage (`--components clock,docker,devices,compose` picks some,
`--non-interactive --yes` runs unattended). On Windows or macOS use `setup.windows.bat` or `./setup.mac.sh`
with Docker Desktop running. `prebuild.sh` picks this host's Compose adapter and writes `.devcontainer/.env`.

The container's `dev` service runs as `roboboat` in `/home/roboboat/roboboat_ws`, with this repository
mounted as the workspace. `~/.config/$ASTRO_CONFIG_DIR` (default `astro`; a profile's `container:` can
rename it) is mounted too, so operator config survives a rebuild.
On the host, put `tools/bin` on `PATH` to use `astro` outside the container.

Then point the machine at your team's profile set and check what it found:

```bash
astro profile sync URL REF          # records the profile repo and ref in ~/.config/astro/profiles.env
astro whoami                        # this machine's host, vessel and station, and how they were found
astro secrets check                 # missing secret names (never values)
```

## astro

`astro` composes plain ROS 2 and holds no robot facts. The vessel profile's `astro:` section points at the
team's files (tests, bag presets, state file, stacks, field repo).

| Commands | What they do |
| --- | --- |
| `up [NAMES] [--with OPT]`, `down`, `ps`, `logs NAME [-f]` | Start, stop, list and tail the vessel's stacks. Each runs on its host in the dev container (private `tmux -L astro`, else `setsid`), so an ssh drop never stops it |
| `on HOST [cmd]` | A shell or one command in a host's dev container (ssh + `docker exec`) |
| `status [--watch]` | Nodes, the latest message on each test topic, recording, last log lines, next test |
| `rec start [PRESET] [LABEL]`, `rec stop`, `rec ls` | Record a bag (mcap, 300 s splits); list today's bags with the log lines inside each |
| `mark TEXT`, `log [-n N]` | Add to, or print, the day's log |
| `test list`, `test status`, `test NAME [--space S]` | Run a test or group in this terminal. Each prints its control layer, old -> new and the exact apply command |
| `apply RESULT`, `undo` | Write a result into the state file (backup first, checked, rolled back on refusal), then set the live parameters; `undo` restores the newest backup |
| `bundle [--bags]` | The state file, today's results and log (and bags) in one tgz, for the sim |
| `sim up\|down\|status\|check` | The sim vessel's world; then `astro on sim` works like a boat |
| `check NAME`, `issue TEXT`, `sync`, `show [DATE]` | The field log: checklists, issues, and the day's log and results pushed to the field repo |
| `NAME [args]` | Any package command from a `help.yaml`, e.g. `astro mix 0.5 0 0` |

A **stack** runs in the background on its host. A **test** runs in your terminal; forwarded from a host
shell over ssh (`docker exec`) it keeps running if the link drops, so RC is the stop.

Outside ROS (no `rclpy`: a host shell), `status`, `rec`, `mark`, `log`, `test`, `apply`, `undo` and
`bundle` run on the profile's `astro.home` (a registry role or host) as `astro on HOME astro ARGS`, so the day
log, bags, results and state stay there; `mark` goes in the background. `--here` (or `ASTRO_HERE=1`) runs
one here. Every other verb runs where it is typed.

### Where things are

| What | Where |
| --- | --- |
| Day's log (`HH:MM:SS  kind  message`) | `DATA/DATE/log.txt` |
| Bags | `DATA/DATE/bags/HHMMSS-LABEL/` |
| Test results (small YAML) | `DATA/DATE/results/` |
| Stack logs | `DATA/DATE/stacks/NAME.log` |
| Bundles | `DATA/DATE/bundle-HHMMSS.tgz` |
| The one file `apply` writes | the profile's `astro.state` |

`DATA` is `$ASTRO_DATA`, else the profile's `astro.data`, else `bags/` in the workspace.

### The profile's astro section

```yaml
astro:
  tests: PKG/config/tests.yaml      # test procedures: topics, axes, spaces, tests, groups
  bags: PKG/config/bags.yaml        # rec presets: {NAME: [topics] | all}
  state: ~/.config/TEAM/state.yaml  # the one file apply writes
  check: ros2 run PKG config_check  # validates the state (path appended); exit 0 = ok
  events: /events                   # every log line is also published here
  home: ROLE|HOST                   # where the ROS-side verbs run when astro is typed outside ROS
  data: null                        # data root; else $ASTRO_DATA
  sim: null                         # the sim backend command (sim profile)
  help: [PATH]                      # more help.yaml files, each its own group
  bundle_next: 'TEXT {bundle}'      # printed after astro bundle
  field: {repo: OWNER/NAME, url: GIT_URL, dir: ~/.local/share/astro/field, auto: true}
  stacks:                           # what astro up starts, in order
    drive: {host: ROLE|HOST, run: 'ros2 launch PKG drive.launch.py', ready: [/topic], needs: [sensors],
            default: true, with: {controller: 'controller:=true'}}   # run: null = started elsewhere
```

**Tests.** The test types are generic (`still`, `couple`, `speed`, `steps`, `nav2`); the tests file names the
topics, axes, state keys and live parameters. A result records the keys it ran on, so `astro test status`
marks it stale when one of them changes.

**Sim.** `astro sim VERB` runs the profile's `astro.sim` command as `CMD up|down|status|check [args]`, with
the sim profile's `ROS_DOMAIN_ID`, `ASTRO_DATA` and `ASTRO_STATE` exported. Any script with those verbs that
starts a world and its ROS bridge works.

**Field log.** `astro.field` names a git repo with `checklists/` and `sessions/DATE-LABEL/`. `astro sync`
copies the day's log, results and a bag index there; on the operator laptop it fetches them from
`astro.home` first (never the bag data), and `show` reads that copy. A forwarded `mark` that cannot reach home
is kept pending locally and sent with its original time by the next forwarded command or `sync`. astro files an `auto` issue when a test aborts with an
error, a stack fails to come up or the check refuses an apply; a repeat adds at most one comment an hour,
and at most 5 issues are filed a day. `auto: false` turns this off for a vessel, `ASTRO_FIELD=off` turns off
all its network calls.

## Profiles, devices and hosts

astro_dock ships no profiles. A profile set is a directory of profiles: a fleet, its vessels and stations,
each child's YAML merged over its parent's (strict: unknown or duplicate keys fail with file and line).
[`tests/data/profiles`](tests/data/profiles) is a complete example. Without one, this is a plain ROS
workspace.

| Command | Does |
| --- | --- |
| `astro whoami`, `astro env [KEY]`, `astro profile show` | Which host and vessel this is; the network env or one of its values; the merged profile |
| `astro device KIND VERB` | A device kind's `status`, `plan`, `verify` (read only), `apply` (typed confirmation; network changes roll back unless verified) or `rollback` |
| `astro station take`, `astro station release` | Make this computer the operator station, or stop that |
| `astro hot REPO [--dry-run]` | Push a repo's working tree to the hosts that run it and build there (LAN only) |
| `astro build`, `astro overlay PATH`, `astro pyenv PATH` | Build the workspace, or a worktree as an overlay, or run a worktree's Python from source |

Secrets live in `~/.config/astro/secrets.env` (mode 600, outside any git tree); environment beats secrets
beats profile.

<details><summary>The low layer: hw.sh</summary>

These commands run [`hw.sh`](hw.sh), the device and fleet script underneath astro. You rarely need it
directly; `./hw.sh KIND --help` shows a device kind's details. Its usage:

```text
hw.sh [--vessel V] [--host H] KIND VERB [ARGS]   (KIND VERB --help for details)
hw.sh whoami [--root|--registry] this machine's host and vessel, and how found (or the profile root or registry)
hw.sh env [KEY]                  the vessel's network env path, or one of its keys or role addresses (<ROLE>_IP)
hw.sh profile show [NAME]        a merged profile (no secrets)
hw.sh profile sync [URL [REF]]   check out your profile set at its ref (URL REF: record them first)
hw.sh secrets check              the secrets file and the missing key names (never values)
hw.sh hot REPO [--host NAME] [--dry-run] push src/REPO's working tree (uncommitted too) to the boat hosts running it, LAN only; branch field/DATE (--dry-run: hosts, files, build plan; sends nothing)
hw.sh station take [--dry-run]   make this computer the operator station (boat link, gateway and time roles, keys, station file, and a heartbeat service if the profile names one)
hw.sh station release            stop this computer's operator heartbeat service, if any
hw.sh overlay PATH [--base REF] [--all] [--dry-run] colcon-build a worktree's packages touched against REF (origin/main) as an overlay on the workspace install, in the dev container; prints the source line
hw.sh pyenv PATH                 the PYTHONPATH that runs a worktree's pure-Python packages from source, no colcon
hw.sh build [--ws DIR] [--container NAME] [-- ARGS] the workspace's colcon build of DIR (in NAME, default its ROS_WS), from clean when its image or colcon defaults changed
hw.sh image [--pull] [--local]   the dev image the Compose config names (--pull: when the registry has a newer one; --local: the local image built on it)
```

</details>

## Containers and images

| Image tag | Platform | For |
| --- | --- | --- |
| `core` | AMD64 / ARM64, Ubuntu 24.04 | General ROS development and MAVROS |
| `cuda` | AMD64, CUDA / ZED | NVIDIA desktops and cameras |
| `jetson-jp6-jazzy` | ARM64, JetPack 6 | Orin camera and GPU host |
| `sitl` | AMD64, ArduPilot | Simulated flight controller (`sitl` Compose profile) |

Images come from [`lunarzdev/astro`](https://hub.docker.com/r/lunarzdev/astro), built by
[`images.yml`](.github/workflows/images.yml); tags keep `-prev` and `-old` rollbacks. Build with
`colcon build` (symlink install); `astro-ros-install NAME` adds a ROS, apt or `pip:` package to the running
container, and [`packages.txt`](.devcontainer/packages.txt) or
[`requirements.txt`](.devcontainer/requirements.txt) keeps it. `config/run.yaml` sets the ROS domain,
discovery range, data root and DDS transport. `./setup.linux.sh --components clock` sets up chrony.

A host without the Dev Container CLI (a Jetson, for example) starts the container directly:

```bash
.devcontainer/prebuild.sh
docker compose -p "$(basename "$PWD")_devcontainer" -f .devcontainer/docker-compose.yml \
  -f .devcontainer/docker-compose.override.yml up -d --pull always dev
```

| Path | Holds |
| --- | --- |
| [`.devcontainer/`](.devcontainer/) | Images, Compose adapters per platform, shell setup |
| [`tools/`](tools/) | `astro` |
| [`hw.sh`](hw.sh), [`devices/`](devices/) | Device drivers and fleet tooling, driven by a profile set |
| [`net/`](net/) | mDNS, shared folder and WARP mesh helpers |
| [`.agents/`](.agents/) | Coding-agent hooks ([`AGENTS.md`](AGENTS.md)) |
| [`src/ros_tcp_endpoint`](https://github.com/MHSeals/ROS-TCP-Endpoint) | Submodule: the ROS bridge to a Unity simulator |
| [`tests/`](tests/) | Unit tests and the example profile set |

## Development

```bash
python3 -m unittest discover -s tests
tests/smoke.sh                 # contacts nothing; must pass before landing
```

Run ROS probes in an isolated container or domain with no actuators attached. Update this README when
setup or usage changes.
