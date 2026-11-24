# astro_dock

A project-agnostic ROS 2 Jazzy container workspace and host tooling for robotics development. Startup uses
prebuilt images from [`lunarzdev/astro`](https://hub.docker.com/r/lunarzdev/astro); application code lives in
the mounted workspace (`src/`, gitignored) and rebuilds independently. Everything specific to one team or
robot (fleet, network, hosts, device settings) lives in a profile set the user supplies, never here.

### Layout

| Path | Responsibility |
| --- | --- |
| [`.devcontainer/`](.devcontainer/) | Images, Compose adapters (Linux, NVIDIA, Jetson, Raspberry Pi, immutable hosts, Windows, macOS), shell setup, validation |
| [`setup.linux.sh`](setup.linux.sh), `setup.mac.sh`, `setup.windows.bat` | Host setup in stages (clock, Docker, devices, Compose adapter, repos, ...) |
| [`hw.sh`](hw.sh), [`devices/`](devices/) | Device drivers and fleet tooling driven by a profile set |
| [`net/`](net/) | mDNS, shared folder and WARP mesh helpers |
| [`.agents/`](.agents/) | Coding-agent hook dispatcher (see [`AGENTS.md`](AGENTS.md)) |
| [`src/ros_tcp_endpoint`](https://github.com/MHSeals/ROS-TCP-Endpoint) | Submodule: ROS connection to the Unity simulator ([`crane_sim`](https://github.com/1unarzDev/crane_sim)) |
| [`tests/`](tests/) | Unit tests and the fixture profile set (`tests/data/profiles`) |

| Image tag | Platform / stack | Purpose |
| --- | --- | --- |
| `core` | AMD64 / ARM64, Ubuntu 24.04 | General ROS development and MAVROS |
| `cuda` | AMD64, Ubuntu 24.04, CUDA / ZED | NVIDIA desktop development and cameras |
| `jetson-jp6-jazzy` | ARM64, JetPack 6 / L4T R36, Jazzy from source, ZED | Orin camera / GPU host |
| `sitl` | AMD64, ArduPilot | Simulated flight controller (`sitl` Compose profile) |

Only JetPack 6 is supported; JetPack 5 (Foxy) tooling lives on the `legacy/jp5-foxy` branch. The `dev` service
runs as `roboboat` in `/home/roboboat/roboboat_ws` with the repository mounted as its workspace.

### Setup

```bash
git clone --recurse-submodules https://github.com/1unarzDev/astro_dock.git && cd astro_dock
./setup.linux.sh --dry-run        # preview, then run without --dry-run
```

`setup.linux.sh` asks before each stage; `--components clock,docker,devices,compose` selects stages and
`--non-interactive --yes` provisions unattended. An NVIDIA driver must already be installed when needed. On
Windows or macOS use `setup.windows.bat` (`/y` for unattended) or `./setup.mac.sh` with Docker Desktop running.
Then, with Node.js and the Dev Container CLI:

```bash
.devcontainer/prebuild.sh
devcontainer up --workspace-folder .
devcontainer exec --workspace-folder . bash
```

`prebuild.sh` copies the host's adapter to the ignored `docker-compose.override.yml` and writes
`.devcontainer/.env`. Container names default to generic values (`astro_dev`, alias `astro`, config directory
`astro`) unless the profile's `hosts.yaml` sets `container:`; `ASTRO_CONTAINER`, `ASTRO_ALIAS` and
`ASTRO_CONFIG_DIR` in the environment win. `~/.config/$ASTRO_CONFIG_DIR` is bind-mounted so operator config
survives rebuilds. `config/run.yaml` (layered with the operator's `run.yaml` and `ASTRO_RUN_CONFIG`) sets the
ROS domain, discovery range, data root and transport; `.devcontainer/run_env.py` loads it strictly.

### Usage

Add ROS packages under `src/` and build from the workspace root (`colcon_defaults.yaml` makes it a
symlink-install). `help` in a container shell browses the commands that astro_dock and each package describe
in a `help.yaml`. `astro-ros-install <name>` installs a ROS, apt or `pip:` package into the running container;
persist it in [`packages.txt`](.devcontainer/packages.txt),
[`jetson-packages.txt`](.devcontainer/jetson-packages.txt) or [`requirements.txt`](.devcontainer/requirements.txt).

```bash
colcon build --symlink-install
ASTRO_ROSDEP_INSTALL=1 .devcontainer/postcreate.sh   # after adding packages
```

**Simulation:** run `crane_sim` on the host and the endpoint in the container
(`ros2 run ros_tcp_endpoint default_server_endpoint --ros-args -p ROS_IP:=0.0.0.0 -p ROS_TCP_PORT:=10000`), and
enable `use_sim_time` for nodes that read the simulator's `/clock`. ArduPilot SITL runs as the `sitl` Compose
profile (`docker compose -f .devcontainer/docker-compose.yml -f .devcontainer/docker-compose.override.yml --profile sitl up -d sitl`).

**DDS:** the domain is the vessel's `ROS_DOMAIN_ID` (from its network env, via prebuild), else 0;
`ASTRO_ROS_DOMAIN_ID` overrides. Shells load [`config/fastdds.xml`](config/fastdds.xml), and on a vessel's
subnet `ros_net` adds unicast static peers.

**Clock:** containers share the host clock. `./setup.linux.sh --components clock` configures chrony
(`ASTRO_NTP_SERVER` / `ASTRO_NTP_ALLOW`; the value `lan` reads the vessel's network env and time-server role).

### Device tooling and profiles

`./hw.sh` drives a robot's network devices and computers from a profile set:

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

Each device kind (`hosts/fleet`, `jetson/l4t`, `lidar/vlp16`, `radio/airmax`, `router/ddwrt` under
[`devices/`](devices/)) has `status`, `plan` and `verify` (read only), `apply` (typed confirmation; network
changes roll back on a timer unless verified) and `rollback`. `./hw.sh KIND --help` lists a kind's verbs.

- **Profile set:** `ASTRO_PROFILES=DIR`, or `./hw.sh profile sync URL REF` to check a git repository out at
  `profiles/` (gitignored); `~/.config/astro/profiles.env` records it. With no profile set, astro_dock is a
  generic ROS workspace and `hw.sh` lists no kinds.
- **Layout:** one directory per profile; the fleet has no `extends:`, vessels and stations extend it, and a
  child's YAML merges over its parent's (strict: unknown or duplicate keys fail with file and line). Each
  `<kind>.yaml` names a driver. [`tests/data/profiles`](tests/data/profiles) is a complete example.
- **Resolution:** the machine finds its registry host by hostname or MAC; `--host` and `--vessel` override.
  Station membership is the `operator` role.
- **Secrets:** `~/.config/astro/secrets.env` (`ASTRO_SECRETS` overrides), mode 600 and outside any git tree;
  `./hw.sh secrets check` lists missing key names. Precedence: environment > secrets > profile.

### Images and deployment

[`images.yml`](.github/workflows/images.yml) builds and publishes the images on native runners;
[`container-checks.yml`](.github/workflows/container-checks.yml) validates adapters, scripts and policy on
every PR. Runtime tags rotate with `-prev` / `-old` rollback generations. `./hw.sh jetson apply user@host`
validates the latest Jetson candidate on a JetPack 6 host and promotes it. For a standalone Jetson:

```bash
.devcontainer/prebuild.sh
docker compose -p "$(basename "$PWD")_devcontainer" -f .devcontainer/docker-compose.yml \
  -f .devcontainer/docker-compose.override.yml up -d --pull always dev
```

A local `core` build adds `-f .devcontainer/docker-compose.build.yml` and runs `build dev`.

### Coding agents

Claude Code and Codex run [`.agents/hook.py`](.agents/hook.py), which passes each session, prompt, command and
edit event to every workspace package's `agent_hook.py`. See [`AGENTS.md`](AGENTS.md).

### Development

Keep changes scoped and update this README when setup, usage or architecture changes. Tests:

```bash
python3 -m unittest discover -s tests
tests/smoke.sh                 # contacts nothing; gates landing
```

Run ROS probes in an isolated container or domain without attached actuators.
