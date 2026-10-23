# astro_dock

### Overview

This repository provides a project-agnostic ROS container workspace and host tooling for robotics development, with sensor drivers and integration for the Unity simulator in [`crane_sim`](https://github.com/1unarzDev/crane_sim). Startup uses prebuilt images from [`lunarzdev/astro`](https://hub.docker.com/r/lunarzdev/astro); application code lives in the mounted workspace and can be rebuilt independently.

The main stack uses ROS 2 Jazzy. JetPack 5 hosts use a separate Foxy image for camera compatibility. Linux, NVIDIA Jetson, Raspberry Pi, Windows, and macOS have Compose adapters; robotics hardware runs on native Linux, while desktop hosts also support development and simulation connections.

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
| `jetson-jp5-foxy` | ARM64, JetPack 5 / L4T R35, Ubuntu 20.04, Foxy, ZED 4.0 | Xavier NX camera / GPU host |
| `sitl` | AMD64, ArduPilot | Simulated FCU |

Jetson images contain camera drivers; MAVROS is provided by `core` for a separate control host. JetPack 6 builds Jazzy from pinned sources because Jazzy's official Ubuntu packages target 24.04. Keep ROS apt repositories matched to the image's Ubuntu release. JetPack 4 / L4T R32 is unsupported, and Foxy is end-of-life.

#### Layers and host overrides

Images supply the platform dependencies; the mounted workspace supplies your application. `core` and `cuda` share [`Dockerfile.dev`](.devcontainer/Dockerfile.dev), using ROS and ZED/CUDA base images respectively. JetPack 6 separates the expensive ROS and sensor builds in [`Dockerfile.deps`](.devcontainer/Dockerfile.deps) from the user, Python environment, and runtime tooling in [`Dockerfile.jetson`](.devcontainer/Dockerfile.jetson). ROS and the ZED wrapper occupy separate cache layers, so wrapper changes can reuse the ROS build. JetPack 5 has its own compatible Dockerfile.

[`docker-compose.yml`](.devcontainer/docker-compose.yml) defines the shared services. The workspace mount resolves relative to this file, so it always mounts the repository regardless of the launcher's working directory. `prebuild.sh` copies a host adapter to the ignored `docker-compose.override.yml`, which Dev Containers merges after the base file. Adapters add display sockets, devices, NVIDIA runtime settings, or immutable-host mounts as needed; The probe preserves the host hostname and maps device group IDs; Jetson detection also selects the JetPack image and DDS domain. Rerunning the probe regenerates this file, so keep persistent changes in an adapter or an additional override.

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

Add your project's ROS packages under `src/` inside the container. [`optional_repos.yaml`](optional_repos.yaml) lists additional repositories available through `./clone_optional.sh`; choose the ones your project needs. Build from the workspace root:

```bash
colcon build --symlink-install
help
```

Container shells load ROS, the image overlays and the workspace at startup, and reload them after a successful `colcon build` or `astro-ros-install`; run `ros_env` to reload by hand.

Camera images already include the ZED wrapper. Only clone it when developing the driver, and match the revision pinned in the corresponding Dockerfile. On JetPack 5, use `humble-v4.0.8` with its recursive submodules; the optional clone helper defaults to `master`, which is incompatible with that image.

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

The hardware package keeps normalized PWM effort separate from Nav2's SI body
velocity. `ros2 run mhseals_testing boat_test` is the test suite: it checks
sensors and TF, runs thruster trials, fits drift, response, stopping, and
combined-axis limits into the shared hardware YAML (with a Nav2 profile to pass
as `navigation.launch.py params_file:=...`), and scores navigation and mission
runs. `--velocity-control` inserts the bounded feedback controller between
`/cmd_vel` and the effort-only PWM driver; review the fit before arming. The
same suite previews against crane_ml with `--profile crane_full_stack`.

For a standalone camera session inside a Jetson container:

```bash
ros2 launch zed_wrapper zed_camera.launch.py \
  camera_model:=zed2i camera_name:=front publish_tf:=false publish_map_tf:=false
# In another container shell:
ros2 launch rosbridge_server rosbridge_websocket_launch.xml
```

In Foxglove, choose **Rosbridge**, connect to `ws://<jetson-host>:9090`, and select an image topic. Inspect `ros2 topic list` because ZED 4 and 5 topic names differ. Keep the unauthenticated connection on a trusted LAN.

For JP5 buoy detection, use `ros2 launch mhseals_nav legacy_zed.launch.py` instead of the two standalone commands. It feeds TensorRT 2D inference into SDK 4 for native 3D boxes (requires the JP5 image's wrapper patch); [mhseals_nav's README](https://github.com/MHSeals/mhseals_nav#architecture) covers the ODROID bridge and the `/objects/map` and RViz marker outputs shared with Unity. The `rviz2` preset uses `/detections/markers` for TF-filtered SDK box outlines and confidence percentages; `/tracked_objects` shows retained semantic tracks.

Foxy uses DDS domain **142**; Jazzy uses **42**. Keep them separate: matching their domain IDs can cause discovery failures and crashes. Use an explicit cross-version bridge for shared data. Jazzy RViz cannot directly view the Foxy camera stream; use Foxglove or a matching Foxy environment. `ASTRO_ROS_DOMAIN_ID` overrides the Jetson default for networks with compatible peers.

#### Lidar DDS transport

VLP16 clouds (~642 KB) use a separate large-data DDS transport. Compose and Jazzy container shells load `config/fastdds.xml` for the normal TF, odometry and RViz graph: its 1400-byte UDP messages avoid IP fragment-buffer stalls when viewing maps and costmaps together.

- `mhseals_nav/config/fastdds_large_data.xml` uses TCP with netmask filtering and TCP_NODELAY. SHM stays disabled here to keep raw and relayed `/points` separate. `sensors.launch.py` applies it to the Velodyne driver and transform only.
- `lidar_relay` runs once on the sensor host as two processes with separate transports. It receives full `/points`, preserves all 16 rings while reducing the payload to 32 KiB, and publishes one reliable `/points` writer on normal DDS for both Nav2 and remote RViz. The hardware sensor launch starts it automatically. Its reader keeps only the latest scan; the output publishes asynchronously so viewer delays stay separate from sensor ingestion.

Viewers only run `rviz2` in a container shell; the helper keeps `config/dark.rviz` and uses Cyclone DDS when installed on Jazzy to avoid redundant traffic across a laptop's multiple interfaces. The core image includes it. No viewer relay or transport export is needed. If an older setup created `astro_lidar_relay`, remove it once with `docker rm -f astro_lidar_relay` to avoid duplicate scans. The checked-in profiles use no device addresses and load again after container recreation or reboot.

To inspect full clouds on the sensor host, set both `FASTRTPS_DEFAULT_PROFILES_FILE` and `FASTDDS_DEFAULT_PROFILES_FILE` to `$ROS_WS/install/share/mhseals_nav/config/fastdds_large_data.xml` in a dedicated shell.

#### Lidar-only RTAB-Map and Nav2 commissioning

The current physical stack publishes VLP16 data as `/points`, not `sensor_msgs/LaserScan` on `/scan`, and the Jetson camera is optional on a separate ROS domain. The checked-in RTAB-Map profile therefore uses `subscribe_scan_cloud: true` and remaps `scan_cloud` to `/points`; RGB/depth are disabled so a stopped camera cannot block map updates. Start mapping and navigation on the ODROID after hardware sensors (which start the local relay) and odometry are running:

```bash
ros2 launch mhseals_nav slam.launch.py sim:=false
ros2 launch mhseals_nav navigation.launch.py sim:=false use_lidar:=true
```

Check the live graph before sending a planner-only goal:

```bash
ros2 topic hz /points
ros2 topic echo /map --once
ros2 topic echo /global_costmap/costmap --once
ros2 action send_goal /compute_path_to_pose nav2_msgs/action/ComputePathToPose \
  "{goal: {header: {frame_id: map}, pose: {position: {x: 6.0, y: 2.3}, orientation: {w: 1.0}}}, planner_id: GridBased}"
```

`ComputePathToPose` exercises the planner and publishes `/plan` without driving a thruster. `/local_plan` is produced only while a controller is following a path. In RViz on the viewer laptop, add `/points`, `/map`, `/global_costmap/costmap`, `/local_costmap/costmap`, `/plan`, `/local_plan`, TF, and TF Static; all use ROS domain 42 and the normal DDS transport.

`./setup.linux.sh --components devices` lists attached serial and camera devices with their owners and modes; rerun it after a JetPack upgrade if permissions change. Use scoped device rules rather than recursive permission changes under `/dev`.

Containers share the host clock. For an isolated network with a reachable NTP server, configure it on each Linux host before running timestamp-sensitive nodes or package updates:

```bash
./setup.linux.sh --components clock
timedatectl status
```

Boat devices reach `time.cloudflare.com` through the gateway laptop; set `ASTRO_NTP_SERVER` only when a local time server is available. Offline hosts need an available time source or battery-backed RTC. Stop control nodes and actuators before clock steps.

If a Linux host has the wrong time, the common systemd recovery path is:

```bash
sudo timedatectl set-ntp on
sudo systemctl enable --now systemd-timesyncd
# Wait until: timedatectl show -p NTPSynchronized --value  -> yes
sudo hwclock --systohc
```

Run `hwclock --systohc` only after synchronization succeeds and only on a host with an RTC. On systems that use `chronyd`, configure or restart that service instead of `systemd-timesyncd`; running `./setup.linux.sh --components clock` is the supported alternative because it selects the existing NTP client and persists its configuration.

#### Router and internet sharing

The boat LAN is defined once in [`config/boat-network.env`](config/boat-network.env): subnet, router, gateway device, DHCP range and DNS. [`router_setup.sh`](router_setup.sh) configures a DD-WRT router over SSH from those values, so DHCP clients use the gateway device as their default route. The script configures DHCP, DNS, and WPA2 Wi-Fi, disables router WAN/NAT, and connects devices through normal LAN ports. Addresses, SSID, and credentials are prompted during setup.

```bash
./router_setup.sh --dry-run
./router_setup.sh
```

Reservations for the boat computers sit above the DHCP pool, so each keeps one address. Once per boat computer, drop any hand-set address and let DHCP supply it, the gateway and DNS (use the wired profile's name and interface):

```bash
nmcli con mod "Wired connection 1" ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.dns ""
nmcli dev reapply <interface>
```

The preview connects and inspects without changing router settings. Applying requires confirmation, saves an NVRAM backup when supported, reboots, and verifies the configuration. SSH keys, trusted host keys, and backups live in the ignored `router_setup_state/` directory; the menu also provides backup restoration.

On the gateway device (any Linux host), give its LAN interface `GATEWAY_IP` and install the persistent forwarding service:

```bash
./setup.linux.sh --components gateway
```

`astro-gateway.service` enables IPv4 forwarding and NATs `BOAT_SUBNET` through the device's normal routing, so boat devices reach the internet through WARP when it is connected and through the default uplink otherwise. Edit the config file and rerun the stage to change the subnet. It names no interfaces, reapplies its rules after Docker or iptables restarts, and survives reboots. Check internet access and DNS from a LAN client afterward.

#### Remote work

[`cx`](https://github.com/1unarzDev/cx) is a recommended companion for working remotely with `astro_dock` on a robot. It provides SSH device management, persistent terminal sessions, file browsing/transfers, and access through an enrolled gateway. It runs on Linux AMD64 and ARM64.

After installing `cx` and preparing the execution host as described in its README, enroll the robot:

```bash
cx add user@robot-host
# If the robot is reachable through an enrolled laptop:
cx add user@robot-host --via laptop
cx
```

Open a terminal or persistent shell on the robot host, change to its `astro_dock` checkout, and use `devcontainer exec --workspace-folder . bash` to enter the ROS workspace. Managed sessions keep running when the viewer disconnects. The router and sharing scripts provide network access; `cx` uses that access for remote work.

#### Refresh and stop

Compose pulls missing images while allowing Dev Containers to use its local UID-adjusted image. To refresh published images, run this from the host before reopening the container:

```bash
docker compose \
  -f .devcontainer/docker-compose.yml \
  -f .devcontainer/docker-compose.override.yml \
  pull dev
```

Keep `ASTRO_PULL_POLICY=missing` during Dev Containers startup; `always` attempts to pull the locally generated UID image from the registry. To stop and remove the services, use the same Compose files with `down`.

### Development

Develop application packages inside the container. After adding packages or changing their manifests, install workspace dependencies explicitly, then build and test the affected packages. Replace `your_package` with your package name:

```bash
ASTRO_ROSDEP_INSTALL=1 .devcontainer/postcreate.sh
colcon build --symlink-install --packages-select your_package
colcon test --packages-select your_package
colcon test-result --verbose
```

On any image, `astro-ros-install <name>` (it adds `sudo -E` in container shells) installs a package by distro-independent name: ROS package names resolve to the image's `ros-$ROS_DISTRO-*` debs, with a source build when no deb exists, and other names install from apt. Source additions live in `/opt/astro-extra`. To persist additions across recreation, add the names to [`packages.txt`](.devcontainer/packages.txt) or [`jetson-packages.txt`](.devcontainer/jetson-packages.txt) and rebuild the image; `name !foxy` skips a package on a distro with no release of it.

For a local `core` image build, add the build override from the host:

```bash
docker compose \
  -f .devcontainer/docker-compose.yml \
  -f .devcontainer/docker-compose.override.yml \
  -f .devcontainer/docker-compose.build.yml \
  build dev
```

This builds `core`; Jetson images use their platform-specific Dockerfiles. On JetPack 6, Jazzy is built from source at the upstream release snapshot named by `ROS2_RELEASE` in [`Dockerfile.deps`](.devcontainer/Dockerfile.deps); bump it to the next `release-jazzy-*` tag of [ros2/ros2](https://github.com/ros2/ros2/tags) to update.

#### Image publishing and deployment

[The image workflow](.github/workflows/images.yml) builds `core` on native hosted AMD64 and ARM64 runners, smoke-tests both, and combines them into a multi-architecture tag. `cuda` and `sitl` use hosted AMD64 runners. JetPack 6 builds `deps` and the runtime candidate on hosted ARM64; its dependency content key skips an unchanged foundation, and registry caching preserves intermediate layers. `deps` is an internal build layer. [JetPack 5](.github/workflows/jetson-jp5.yml) builds independently on hosted ARM64. Image compilation uses native runners rather than QEMU.

For a persistent ARM64 build server, register a GitHub runner with `self-hosted`, `linux`, `ARM64`, and `arm64-builder` labels, then dispatch the image workflow with `arm64_builder=self-hosted`. This switches the JetPack 6 jobs; normal automatic builds use hosted runners. Publishing requires `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` secrets, with Read, Write, and Delete access for retention. [Pull-request checks](.github/workflows/container-checks.yml) validate adapters, scripts, dependency locks, and image policy, then build `core` without publishing.

Runtime tags select the current validated image, with `-prev` and `-old` retaining two rollback generations. `-next` and candidate slots are staging references. Both Jetson families use the on-demand [promotion helper](promote_jetson.sh) from a machine with Bash, SSH, `jq`, and an authenticated GitHub CLI:

```bash
./promote_jetson.sh jp5
./promote_jetson.sh jp6 user@your-jp6-host
```

The helper selects the latest successful `main` build's immutable candidate, connects through `ssh tranquility` using Tranquility's keys, and checks ROS, non-root CUDA, and three live ZED frames on the Jetson. It then waits for [hosted CI](.github/workflows/promote-jetson.yml) to verify the build digest and rotate that family's tags, and pulls the release on the Jetson. It installs no services and leaves running containers unchanged. JP5 defaults to `roboboat@squirtle-jetson.local`; JP6 requires a compatible L4T R36 Jetson. Set `ASTRO_JETSON_GATEWAY` to another SSH gateway, or empty for a direct connection. An optional third argument selects a successful build run; candidate artifacts expire after 30 days, so rebuild if needed.

Stop any existing camera publisher before validation. The probe uses isolated ROS domain 223 and removes its temporary container and helper. A failed check prevents promotion; repeating a promotion preserves rollback history. Docker Hub credentials stay in GitHub CI. For a manual diagnostic, run `.devcontainer/boat-validate.sh lunarzdev/astro@sha256:DIGEST` on either matching Jetson. Use the Compose command below to deploy after promotion.

For standalone deployment on either Jetson generation, run the same Compose service VS Code uses from the repository root. `prebuild.sh` selects the JetPack 5 (Foxy) or 6 (Jazzy) image and domain, and the Jetson adapter restarts the container after reboots:

```bash
.devcontainer/prebuild.sh
docker compose -p astro_dock_devcontainer -f .devcontainer/docker-compose.yml -f .devcontainer/docker-compose.override.yml up -d --pull always dev
.devcontainer/boat-validate.sh --container roboboat_dev
docker exec -it roboboat_dev bash
```

Rerun the `up` command to update; `--pull always` replaces a stale local image. For offline deployment, transfer the ARM64 image with `docker save` / `docker load` and drop `--pull always`.

The [retention workflow](.github/workflows/registry-retention.yml) keeps bounded candidate slots and release history. Inspect registry state without mutations using `.github/scripts/registry-retention.py --repository lunarzdev/astro audit`.

Keep changes scoped, explain their purpose and validation in the pull request, and update this README when setup, usage, or architecture changes. Run ROS probes in an isolated container/domain without attached actuators.
