# astro_dock

### Overview

This repository provides a project-agnostic ROS container workspace and host tooling for robotics development, with sensor drivers and integration for the Unity simulator in [`crane_sim`](https://github.com/1unarzDev/crane_sim). Startup uses prebuilt images from [`lunarzdev/astro`](https://hub.docker.com/r/lunarzdev/astro); application code lives in the mounted workspace and can be rebuilt independently.

The stack uses ROS 2 Jazzy. Linux, NVIDIA Jetson, Raspberry Pi, Windows, and macOS have Compose adapters; robotics hardware runs on native Linux, while desktop hosts also support development and simulation connections.

### Architecture

The `dev` service runs as `roboboat` in `/home/roboboat/roboboat_ws`, with the repository mounted as its workspace. Development containers are privileged; native Linux adapters expose `/dev` for hardware access.

| Component | Responsibility |
| --- | --- |
| [`.devcontainer/`](.devcontainer/) | Runtime images, Compose adapters, shell setup, deployment, and validation |
| [`ROS-TCP-Endpoint`](https://github.com/MHSeals/ROS-TCP-Endpoint) | ROS connection to the Unity simulator; included as a submodule |
| [`crane_sim`](https://github.com/1unarzDev/crane_sim) | Unity physics and simulated sensors; runs separately |
| `sitl` service | Optional ArduPilot simulated flight controller; AMD64 image |

Runtime images separate the host capabilities:

| Image tag | Platform / stack | Purpose |
| --- | --- | --- |
| `core` | AMD64 / ARM64, Ubuntu 24.04, Jazzy | General ROS development and MAVROS |
| `cuda` | AMD64, Ubuntu 24.04, Jazzy, CUDA / ZED | NVIDIA desktop development and cameras |
| `jetson-jp6-jazzy` | ARM64, JetPack 6 / L4T R36, Ubuntu 22.04, Jazzy, ZED 5.4 | Orin camera / GPU host |
| `sitl` | AMD64, ArduPilot | Simulated FCU |

Jetson images contain camera drivers; MAVROS is provided by `core` for a separate control host. JetPack 6 builds Jazzy from pinned sources because Jazzy's official Ubuntu packages target 24.04. Keep ROS apt repositories matched to the image's Ubuntu release. Only JetPack 6 / L4T R36 is supported; JetPack 5 (Foxy) tooling lives on the `legacy/jp5-foxy` branch.

#### Layers and host overrides

Images supply the platform dependencies; the mounted workspace supplies your application. `core` and `cuda` share [`Dockerfile.dev`](.devcontainer/Dockerfile.dev), using ROS and ZED/CUDA base images respectively. JetPack 6 separates the expensive ROS and sensor builds in [`Dockerfile.deps`](.devcontainer/Dockerfile.deps) from the user, Python environment, and runtime tooling in [`Dockerfile.jetson`](.devcontainer/Dockerfile.jetson). ROS and the ZED wrapper occupy separate cache layers, so wrapper changes can reuse the ROS build.

[`docker-compose.yml`](.devcontainer/docker-compose.yml) defines the shared services. `prebuild.sh` copies a host adapter to the ignored `docker-compose.override.yml`, which Dev Containers merges after the base file. Adapters add display sockets, devices, NVIDIA runtime settings or immutable-host mounts; the probe also preserves the host hostname, maps device group IDs, and on Jetson requires JetPack 6. Rerunning it regenerates the override, so keep persistent changes in an adapter or an additional override.

`~/.config/$ASTRO_CONFIG_DIR` on the host (created by `prebuild.sh`) is bind-mounted at `/home/roboboat/.config/$ASTRO_CONFIG_DIR`, so operator config survives container rebuilds. The container's names are generic (`astro_dev`, hostname alias `astro`, config directory `astro`) unless the profile's `hosts.yaml` sets `container: {name, hostname, config_dir}`: `prebuild.sh` writes them (`./hw.sh hosts compose`) to `.devcontainer/.env` as `ASTRO_CONTAINER`, `ASTRO_HOSTNAME` and `ASTRO_CONFIG_DIR`, which Compose reads; the same variables in the environment win.

Extend the environment at the layer that owns the change: add application packages under `src/`, reusable dependencies to [`packages.txt`](.devcontainer/packages.txt) (ROS package names such as `nav2_bringup`, or apt package names; no distro prefix) or [`requirements.txt`](.devcontainer/requirements.txt) (pip), or Jetson-only packages to [`jetson-packages.txt`](.devcontainer/jetson-packages.txt), and host mounts or services to a Compose override. Add another `-f` file after the base and host adapter for project-specific settings; later files override earlier values according to Compose's merge rules. For Dev Containers, include that file in `devcontainer.json`'s `dockerComposeFile` list. `ASTRO_IMAGE` selects a custom runtime image without changing the adapter.

### Setup

Clone the workspace, including its ROS TCP submodule:

```bash
git clone --recurse-submodules https://github.com/1unarzDev/astro_dock.git
cd astro_dock
# For an existing checkout:
git submodule update --init --recursive
```

On Linux, preview the host setup before applying it:

```bash
./setup.linux.sh --dry-run
./setup.linux.sh
```

The script configures host time synchronization, Docker, NVIDIA container support, scoped device rules, development tools, and the Compose adapter. It asks before each stage and supports Debian/Ubuntu, Arch, Fedora, RHEL derivatives, immutable Fedora hosts, and Ubuntu Core. Use `--components clock,docker,devices,compose` to select stages, or `--non-interactive --yes` for provisioning. An NVIDIA driver must already be installed when needed.

On Windows or macOS, use `setup.windows.bat` or `./setup.mac.sh` and keep Docker Desktop running. Container GUI applications additionally need [VcXsrv](https://sourceforge.net/projects/vcxsrv/) on Windows or [XQuartz](https://www.xquartz.org/) on macOS, with network connections enabled. Graphics and hardware access depend on the host; use native Linux for robotics hardware.

For manual setup, install [Docker Engine](https://docs.docker.com/engine/install/) with Compose, Node.js LTS, and the [Dev Container CLI](https://github.com/devcontainers/cli). NVIDIA hosts also need the NVIDIA Container Toolkit. VS Code with the Dev Containers extension is optional.

Install Node.js LTS if needed; the setup scripts do not install it. Then run from the repository root on Linux, macOS, or WSL:

```bash
npm install --global @devcontainers/cli
.devcontainer/prebuild.sh
devcontainer up --workspace-folder .
devcontainer exec --workspace-folder . bash
```

On native Windows, the setup script generates the Windows adapter; install the CLI with the same `npm` command and run the `devcontainer` commands from that checkout. VS Code's **Reopen in Container** uses the same configuration after the adapter is generated.

### Usage

Add your project's ROS packages under `src/` inside the container. With a device profile (below), its `hosts.yaml` names the repositories each registry role checks out (`repos:` keyed by role, plus `all`) and where they come from (`sources:`). `./setup.linux.sh --components repos [--vessel V] [--host H] [--role R]` clones the missing ones for this computer and builds them; `./hw.sh hosts repos [--role ROLE...]` previews the list. Build from the workspace root:

```bash
colcon build --symlink-install
help
```

Container shells load ROS, the image overlays and the workspace at startup, and reload them after a successful `colcon build` or `astro-ros-install`; run `ros_env` to reload by hand. `help` browses the commands that astro_dock and each workspace package describe in a `help.yaml` (package root or `share/<package>/`), fills in their parameters, and runs one after you confirm it; `help --list` prints them as text. `colcon_defaults.yaml` makes every build from the workspace root a symlink-install, so Python, YAML and launch edits are live; an interactive shell warns when `install/` holds old copies of `src/` from a build without it (`.devcontainer/install-check.py`).

Camera images already include the ZED wrapper. Only clone it when developing the driver, and match the revision pinned in the corresponding Dockerfile.

#### Simulation

Run [`crane_sim`](https://github.com/1unarzDev/crane_sim) on the host. Start the ROS TCP endpoint inside the container:

```bash
ros2 run ros_tcp_endpoint default_server_endpoint --ros-args \
  -p ROS_IP:=0.0.0.0 -p ROS_TCP_PORT:=10000
```

The endpoint listens on all container interfaces at port `10000`. In the simulator's ROS connection settings, use the development host's reachable address and the same port. Launch your project's ROS nodes separately, enabling `use_sim_time` for nodes that consume the simulator's `/clock`. A project launch file may already start the endpoint; start each component once.

If testing with ArduPilot SITL, start its Compose profile from a host terminal:

```bash
docker compose \
  -f .devcontainer/docker-compose.yml \
  -f .devcontainer/docker-compose.override.yml \
  --profile sitl up -d sitl
docker exec -it ardupilot_sitl bash
```

Inside `ardupilot_sitl`, with the Unity connection running:

```bash
Tools/autotest/sim_vehicle.py -v "$VEHICLE" $SITL_EXTRA_ARGS
```

Connect MAVROS from the development container using the TCP output configured by the SITL service:

```bash
ros2 launch mavros apm.launch fcu_url:=tcp://127.0.0.1:5762
```

If your project launch already starts MAVROS, configure that instance instead. The SITL image is AMD64; running it on ARM64 requires emulation.

#### Hardware and cameras

On physical hardware, launch your project's nodes on the hosts responsible for their devices. Disable simulation time and synchronize clocks across hosts so sensor timestamps and TF agree. Device configuration and actuator control belong to the project using the workspace.

For a standalone camera session inside a Jetson container:

```bash
ros2 launch zed_wrapper zed_camera.launch.py \
  camera_model:=zed2i camera_name:=front publish_tf:=false publish_map_tf:=false
```

The DDS domain is the vessel's `ROS_DOMAIN_ID` (its network env; prebuild prints it), which prebuild injects into the adapter with the vessel itself (`ASTRO_VESSEL`, so the container keeps it off the LAN), else ROS 2's default 0 (the vessel's profile is the one source of a domain); `ASTRO_ROS_DOMAIN_ID` overrides it (prebuild and Compose both read it). An operator laptop that resolves no vessel at prebuild (off the boat LAN, or `--host NAME` without `--vessel`), and a stale adapter keep the default, and prebuild warns: export `ASTRO_ROS_DOMAIN_ID` (the vessel's `ROS_DOMAIN_ID`) where the dev container opens, or rerun prebuild with `ASTRO_VESSEL=NAME`. A checkout without profiles keeps it quietly (generic mode). `/usr/local/zed/tools/ZED_Explorer --all` lists attached cameras.

Compose and Jazzy shells load [`config/fastdds.xml`](config/fastdds.xml) (1400-byte UDP messages avoid IP fragment-buffer stalls with large topics; a 60 s discovery lease rides out radio dropouts). On a vessel's subnet (`LAN_SUBNET` of its network env, or `ASTRO_DDS_SUBNET`), `ros_net` also sets `ROS_STATIC_PEERS` to the gateway and the reserved hosts, so discovery reaches them by unicast; multicast stays on and a user-set value wins.

`./setup.linux.sh --components devices` lists attached serial and camera devices with their owners and modes; rerun it after a JetPack upgrade if permissions change. Use scoped device rules rather than recursive permission changes under `/dev`.

Containers share the host clock. If a Linux host has the wrong time, `./setup.linux.sh --components clock` is the supported fix: it selects the existing NTP client and persists its configuration. For an isolated network, configure it on each host before running timestamp-sensitive nodes or package updates:

```bash
./setup.linux.sh --components clock
timedatectl status
```

`ASTRO_NTP_SERVER` names the servers to follow and `ASTRO_NTP_ALLOW` a subnet to serve (chrony; the stage installs it and disables `systemd-timesyncd`, and a rerun changes nothing). The value `lan` reads the vessel's network env: `ASTRO_NTP_SERVER=lan` follows the registry host with the `time_server` role, `ASTRO_NTP_ALLOW=lan` serves `LAN_SUBNET` (also following the `operator` role's hosts), and `ASTRO_NTP_ALLOW=operator` serves it only while this host is itself synchronized. `lan` refuses a vessel without a time server. `.devcontainer/astro-clock.py` prints every host's offset. Stop control nodes and actuators before clock steps.

The `jetson` stage sets a JetPack 6 Jetson's persistent MAXN_SUPER power mode, a 16 GB `/swapfile` and the headless multi-user target; `docker` and `gpu` keep JetPack's Docker and toolkit and add the user to `docker`.

#### Device tooling

`./hw.sh` drives the robot's network devices and computers from a device profile:

```text
hw.sh [--vessel V] [--host H] KIND VERB [ARGS]   (KIND VERB --help for details)
hw.sh whoami [--root|--registry] this machine's host and vessel, and how found (or the profile root or registry)
hw.sh env [KEY]                  the vessel's network env path, or one of its keys or role addresses (<ROLE>_IP)
hw.sh profile show [NAME]        a merged profile (no secrets)
hw.sh profile sync [URL [REF]]   check out your profile set at its ref (URL REF: record them first)
hw.sh secrets check              the secrets file and the missing key names (never values)
hw.sh hot REPO [--host NAME] [--dry-run] push src/REPO's working tree (uncommitted too) to the boat hosts running it, LAN only; branch field/DATE (--dry-run: hosts, files, build plan; sends nothing)
hw.sh station take [--dry-run]   make this computer the operator station (boat link, gateway and time roles, keys, station file, heartbeat service)
hw.sh station release            stop this computer's operator heartbeat service
hw.sh overlay PATH [--base REF] [--all] [--dry-run] colcon-build a worktree's packages touched against REF (origin/main) as an overlay on the workspace install, in the dev container; prints the source line
hw.sh pyenv PATH                 the PYTHONPATH that runs a worktree's pure-Python packages from source, no colcon
hw.sh build [--ws DIR] [--container NAME] [-- ARGS] the workspace's colcon build of DIR (in NAME, default its ROS_WS), from clean when its image or colcon defaults changed
hw.sh image [--pull] [--local]   the dev image the Compose config names (--pull: when the registry has a newer one; --local: the local image built on it)
```

`./hw.sh --help` (or `help`) lists the kinds of the selected profile, `./hw.sh KIND --help` their verbs and `./hw.sh COMMAND --help` a command's usage. Every driver has these:

| Verb | Does | Changes the device |
| --- | --- | --- |
| `status` | current state | never |
| `plan` | what `apply` would change | never |
| `apply` | the change, after a typed confirmation | yes; network changes roll back on a timer unless verified or kept |
| `verify` | state against the profile | never |
| `rollback` | back to the saved or previous state | yes |

- **Drivers:** `devices/<kind>/<driver>.sh` must define `driver_status`, `driver_plan`, `driver_apply`, `driver_verify` and `driver_rollback`; `hw.sh` refuses a driver missing one. Other `driver_<verb>` functions are extra verbs. astro_dock ships `hosts/fleet`, `jetson/l4t`, `lidar/vlp16`, `radio/airmax` and `router/ddwrt`.
- **Fleet deploy:** `./hw.sh hosts apply [--image REF] [HOST...]` backs up dirty state under `~/.local/state/astro_dock/hosts/` on each ssh host of the vessel's registry, fast-forwards astro_dock and the host's repositories to `origin/main`, checks out this computer's profile set at its commit in the host's `profiles/` (when that set is a git checkout with an origin; `verify` compares it), prunes stale symlink-install links and builds in the dev container. `--image` first commits the running container as `astro-rollback:STAMP` and recreates it on REF. `status`, `plan` and `verify` only read; `rollback IMAGE [HOST...]` recreates on a rollback image and builds; `keys` installs the operator ssh key (also `./setup.linux.sh --components operator-key`).
- **Field hot patch:** `./hw.sh hot REPO [--host NAME]` snapshots this computer's `src/REPO` working tree (uncommitted and untracked files too; the checkout itself is untouched) and sends it as a git bundle over ssh, LAN only, to the boat hosts whose roles run REPO (registry ssh hosts without the `operator` role). Each host backs up its dirty state, checks the snapshot out on `field/YYYYMMDD` and builds only the touched packages; edits of existing `.py`, `.yaml` and launch files skip colcon (live under symlink-install), and docs and tests (`README*`, `.md`, `.rst`, `doc/`, `test/`) never build. `--dry-run` prints the hosts, the files against `origin/main` and the build plan, and contacts no host. Land the change through git later; `hosts apply` returns the hosts to `main`.
- **Operator station:** `./hw.sh station take [--dry-run]` makes this computer the operator station. It stops at once with "reboot needed" when the running kernel's modules are gone, finds the boat link by the vessel's `LAN_SUBNET`, runs setup's `clock` (`ASTRO_NTP_ALLOW=operator`) and `gateway` stages, installs the operator key on the boat hosts and in the dev container, and writes the station file (`station_file` of `hosts.yaml`, a path under `~/.config`: `operator`, `since`, `roles`) here and on every boat host. Last, when `hosts.yaml` names an `operator_heartbeat` command, it installs and starts that command as the systemd user service `astro-operator-heartbeat` (`devices/heartbeat.sh`: run in the dev container as this station's registry host, on the vessel's `ROS_DOMAIN_ID`), so the robot hears the station wherever the TUI runs; it comes back with the dev container and the login. `./hw.sh whoami` shows its state and `./hw.sh station release` stops it. Without the key, `hot` and `station take` reach hosts by registry name.
- **Worktrees in place:** the dev container mounts `~/worktrees` and `~/.cache/wt` at their host paths, and prebuild mounts this workspace at its host path too, so a worktree's git works inside. `./hw.sh overlay PATH` (in the dev container) builds only the worktree's packages touched against `origin/main` (`--base REF`; all of them with `--all` or when git cannot tell) into `PATH/install`, an overlay on the workspace install: symlink-install, and ccache for CMake packages. It prints the `source PATH/install/setup.bash` line; `--dry-run` prints the colcon command only. Pure-Python packages need no colcon: `PYTHONPATH=$(./hw.sh pyenv PATH) python3 -m pytest PATH` runs their tests from source.

#### Profiles

Everything specific to one robot team (its fleet, vessels, network, hosts and device settings) lives in a profile set the user supplies, never in astro_dock. `profiles/` is gitignored, like `src/*`.

- **Source:** `~/.config/astro/profiles.env` (`$ASTRO_PROFILES_CONFIG` overrides; a set variable wins over the file) names it, next to `secrets.env`. `ASTRO_PROFILES=DIR` uses your own directory as it is. Else `ASTRO_PROFILES_URL` (a git repository) at `ASTRO_PROFILES_REF` (a commit, tag or branch; default `main`) is checked out at `profiles/`: `./hw.sh profile sync URL [REF]` records both and checks out, `./hw.sh profile sync` moves to the recorded ref (a branch's latest), and any `hw.sh` command clones it on first use. Nothing else fetches; `profiles/` away from the ref warns, and sync refuses local changes (push them to the profile repository). `./hw.sh whoami` prints the source and commit.
- **Generic mode:** with no profile set, astro_dock is a generic ROS workspace: `hw.sh` lists no kinds and its device verbs and `env` say how to configure one, the dev container keeps the default domain, and setup skips the repos, gateway, boat and jetson stages with one note.
- **Migration:** a checkout from the submodule era keeps its populated `profiles/` after the pull and works as before; run `./hw.sh profile sync URL REF` once to record its source, then rerun `.devcontainer/prebuild.sh`, which puts the vessel's network env and registry (`ASTRO_NETWORK_ENV`, `ASTRO_REGISTRY`) in the dev container's environment; shells also export them. There are no `config/` links into a profile: a container created before that gets the paths only in its shells (`dev.bashrc`), so recreate it.
- **Layout:** one directory per profile. The fleet is the one profile without `extends:`; each other profile is a member (`kind: vessel` or `kind: station`) that extends it. `profile.yaml` holds `extends`, `kind`, `network_env` (the network env file) and `registry` (the host registry), paths relative to its directory, and may carry a member's few device facts as `<kind>:` mappings (`lidar: {mac: ...}`) instead of one-key files. Each `<kind>.yaml` picks a driver (`driver: hosts/fleet` runs `devices/hosts/fleet.sh`) and holds its settings (`$KEY` reads the network env, which holds addresses only; who does what is a registry role, and each role reads as `$<ROLE>_IP`, its hosts' addresses: `./hw.sh env TIME_SERVER_IP`); the fleet's `hosts.yaml` lists repositories by role, `topology.yaml` the members and links, and the optional `secrets.keys` the secret key names. [`tests/data/profiles`](tests/data/profiles) is a complete example: fleet `fleet`, vessels `a` and `b`, station `s`.
- **Merging:** a child's YAML merges over its parent's: mappings key by key, lists and scalars replace; it may set only keys a parent defines, except under a parent's empty mapping (`{}`) or null key. Every profile loads and is checked on every run; duplicate keys, anchors, cycles and unknown keys fail with file and line. `./hw.sh profile show [NAME]` prints the merged result.
- **Resolution:** the machine finds its own registry host, by hostname or by a permanent MAC in the network env's `STATIC_LEASES`, and the vessel that host belongs to. `--host H` (`ASTRO_HOST`) and `--vessel V` (`ASTRO_VESSEL`) override; an unregistered machine passes `--vessel` for device verbs and `env`. `./hw.sh whoami` prints the result and how it was found.
- **Control stations:** station membership is a role, not a name. A registry host with the `operator` role resolves to the fleet's station member (the only one, else the one `topology.yaml` links to the vessel). An operator laptop no registry lists joins as a generic control station (roles `operator`, so `hosts repos` gives `repos.all` and `repos.operator`): by `--host NAME`, without a vessel unless `--vessel` gives one, or by an address inside exactly one vessel's `LAN_SUBNET`, which picks that vessel. A boat computer never joins as a station: a device-tree model (a Jetson or another board), a hostname or permanent MAC of a registry host without the operator role, or a `STATIC_LEASES` or registry address of a boat host keeps it unmatched.
- **Secrets:** `~/.config/astro/secrets.env` (`$ASTRO_SECRETS` overrides), refused unless it is mode 600 and, symlinks followed, outside every git work tree. A YAML names its secrets' keys under `credentials:`, never the values; `./hw.sh secrets check` lists the missing names.
- **Layers:** environment > secrets file > profile. `HW_<PATH>` overrides a YAML value (`HW_MEASURE_SECONDS=3`).

#### Remote work

[`cx`](https://github.com/1unarzDev/cx) is a recommended companion for working remotely with `astro_dock` on a robot. It provides SSH device management, persistent terminal sessions, file browsing/transfers, and access through an enrolled gateway. It runs on Linux AMD64 and ARM64.

After installing `cx` and preparing the execution host as described in its README, enroll the robot:

```bash
cx add user@robot-host
# If the robot is reachable through an enrolled laptop:
cx add user@robot-host --via laptop
cx
```

Open a terminal or persistent shell on the robot host, change to its `astro_dock` checkout, and use `devcontainer exec --workspace-folder . bash` to enter the ROS workspace. Managed sessions keep running when the viewer disconnects.
`./setup.linux.sh --components mdns` sets up mDNS (`HOST.local` names; the devtools and boat stages include it) and [`net/share.sh`](net/share.sh) a shared network folder; [`net/mesh.sh`](net/mesh.sh) joins or runs a WARP mesh on your own Cloudflare account (`net/mesh.sh join <endpoint>/<code>`; see cx's mesh guide); `cx` uses the network access they and the router provide.

To copy session bags off a host into `bags/<name>`, run `./fetch_bags.sh host:~/astro_dock/bags/<session> <name>`. It reads the remote only, and streams a tar over ssh when the remote has no rsync.

#### Refresh and stop

To refresh published images, run this from the host before reopening the container:

```bash
docker compose \
  -f .devcontainer/docker-compose.yml \
  -f .devcontainer/docker-compose.override.yml \
  pull dev
```

Keep `ASTRO_PULL_POLICY=missing` during Dev Containers startup; `always` would try to pull the locally generated UID image. Use the same Compose files with `down` to stop and remove the services.

### Development

Develop application packages inside the container. After adding packages or changing their manifests, install workspace dependencies explicitly, then build and test the affected packages. Replace `your_package` with your package name:

```bash
ASTRO_ROSDEP_INSTALL=1 .devcontainer/postcreate.sh
colcon build --symlink-install --packages-select your_package
colcon test --packages-select your_package
colcon test-result --verbose
```

On any image, `astro-ros-install <name>` (it adds `sudo -E` in container shells) installs a package by distro-independent name: ROS package names resolve to the image's `ros-$ROS_DISTRO-*` debs, with a source build when no deb exists, other names install from apt, and `pip:SPEC` installs into `/workspace/venv`. Source additions live in `/opt/astro-extra`. Each install is recorded in the gitignored `.devcontainer/installed.local.txt`; to persist additions across recreation, move those names to [`packages.txt`](.devcontainer/packages.txt), [`jetson-packages.txt`](.devcontainer/jetson-packages.txt) or [`requirements.txt`](.devcontainer/requirements.txt) and rebuild the image.

For a local `core` image build, add the build override from the host:

```bash
docker compose \
  -f .devcontainer/docker-compose.yml \
  -f .devcontainer/docker-compose.override.yml \
  -f .devcontainer/docker-compose.build.yml \
  build dev
```

This builds `core`; Jetson images use their platform-specific Dockerfiles. On JetPack 6, Jazzy is built from source at the upstream release snapshot named by `ROS2_RELEASE` in [`Dockerfile.deps`](.devcontainer/Dockerfile.deps); bump it to the next `release-jazzy-*` tag of [ros2/ros2](https://github.com/ros2/ros2/tags) to update.

#### Coding agents

Claude Code ([`.claude/settings.json`](.claude/settings.json)) and Codex ([`.codex/hooks.json`](.codex/hooks.json)) run one stdlib dispatcher, [`.agents/hook.py`](.agents/hook.py), on session start and around every shell command. It converts each CLI's event into one neutral event and passes it to the `agent_event` of every workspace package that ships an `agent_hook.py` module (`src/<repo>/<package>/agent_hook.py`). A package hook can add a playbook at session start, deny a command with a reason, or record what ran; `python3 .agents/hook.py --playbook` prints the playbooks in full. Without a package hook, nothing happens. [`AGENTS.md`](AGENTS.md) covers agents that don't run hooks. Codex runs project hooks only after you trust them (`/hooks`). Until then nothing is blocked before it runs, and only the robot's own safeguards apply. Sessions started inside a nested `src/<pkg>` repository don't pick up astro_dock's `.claude` and `.codex` hooks, so start agents from the astro_dock root. Errors go to `~/.local/state/astro/agents-hook.log` and never block a command; all package hooks together get a 2 s budget. Run `python3 -m pytest tests/test_agents_hook.py` to test the dispatcher.

#### Image publishing and deployment

[The image workflow](.github/workflows/images.yml) builds `core` on native hosted AMD64 and ARM64 runners, smoke-tests both, and combines them into a multi-architecture tag. `cuda` and `sitl` use hosted AMD64 runners. JetPack 6 builds `deps` and the runtime candidate on hosted ARM64; its dependency content key skips an unchanged foundation, and registry caching preserves intermediate layers. `deps` is an internal build layer. Image compilation uses native runners rather than QEMU.

For a persistent ARM64 build server, register a GitHub runner with `self-hosted`, `linux`, `ARM64`, and `arm64-builder` labels, then dispatch the image workflow with `arm64_builder=self-hosted`. This switches the JetPack 6 jobs; normal automatic builds use hosted runners. Publishing requires `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` secrets, with Read, Write, and Delete access for retention. [Pull-request checks](.github/workflows/container-checks.yml) validate adapters, scripts, dependency locks, and image policy, then build `core` without publishing.

Runtime tags select the current validated image, with `-prev` and `-old` as rollback generations; `-next` and candidate slots are staging. Promote the Jetson image from a machine with Bash, SSH, `jq` and an authenticated GitHub CLI:

```bash
./hw.sh jetson apply user@your-jp6-host
```

The helper takes the latest successful `main` build's candidate (artifacts expire after 30 days; an optional second argument picks a run), validates it on the Jetson over SSH (ROS, non-root CUDA, live ZED frames; isolated domain 223; stop any camera publisher first; reuses the dev container's `zed_resources` and `zed_settings` volumes, so start the dev container once beforehand), then waits for [hosted CI](.github/workflows/promote-jetson.yml) to rotate the tags. `jetson.yaml` sets the family and the SSH gateway (`HW_GATEWAY=` for direct); the host must run L4T R36. `plan` only resolves the candidate, `verify` checks the host's tag, `rollback HOST RUN_ID` promotes an earlier build. Manual diagnostic: `.devcontainer/boat-validate.sh lunarzdev/astro@sha256:DIGEST`.

For standalone deployment on a Jetson, run the same Compose service VS Code uses from the repository root. `prebuild.sh` selects the JetPack 6 adapter (warning when the dev container runs an image older than its tag), and Compose restarts it after reboots unless it was stopped:

```bash
.devcontainer/prebuild.sh
docker compose -p astro_dock_devcontainer -f .devcontainer/docker-compose.yml -f .devcontainer/docker-compose.override.yml up -d --pull always dev
.devcontainer/boat-validate.sh --container astro_dev   # or the ASTRO_CONTAINER of .devcontainer/.env
docker exec -it astro_dev bash
```

Rerun `up` to update; for offline deployment, `docker save` / `docker load` the ARM64 image and drop `--pull always`.

The [retention workflow](.github/workflows/registry-retention.yml) keeps bounded candidate slots and release history. Inspect registry state without mutations using `.github/scripts/registry-retention.py --repository lunarzdev/astro audit`.

Keep changes scoped, explain their purpose and validation in the pull request, and update this README when setup, usage, or architecture changes. Run ROS probes in an isolated container/domain without attached actuators.

### Static run YAML (off-boat)

`config/run.yaml` holds defaults. `prebuild.sh` layers the operator `~/.config/$ASTRO_CONFIG_DIR/run.yaml` and then `--config /absolute/run.yaml` (or `ASTRO_RUN_CONFIG`) over it, and `python3 -I .devcontainer/run_env.py` projects the result into the Compose override's `dev` environment. Linux/Jazzy only. Example:

```yaml
ros:
  domain_id: 73
  discovery_range: LOCALHOST
data:
  root: /home/roboboat/roboboat_ws/bags/experiment
transport:
  mode: static-fastdds
```

- Strict loading: only these four typed keys; unknown or duplicate keys, anchors, aliases and merge keys refuse with file and line.
- Environment overrides: `ASTRO_ROS_DOMAIN_ID` or `ROS_DOMAIN_ID` (not both), `ROS_AUTOMATIC_DISCOVERY_RANGE`, `ASTRO_DATA` (the data root; `data.root` is relative to the workspace unless absolute). Explicit transport controls (`RMW_IMPLEMENTATION`, DDS profiles, `ASTRO_DDS_*`) refuse.
- `data.root` must sit under the workspace bind. Quote `"OFF"`.
- Changes take effect after prebuild and container recreation. The DDS address and subnet stay in the vessel's network env (`./hw.sh env`) and `config/fastdds.xml`.
