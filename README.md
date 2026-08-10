## Introduction

Welcome to the MHSeals ROS 2 Jazzy development environment. The same Compose project runs on native Linux, NVIDIA Jetson, Raspberry Pi, immutable Fedora-family hosts, Windows, and macOS. Normal startup pulls prebuilt images from `lunarzdev/astro` instead of rebuilding ROS, Gazebo, CUDA, ZED, or ArduPilot locally.

## Installation

Clone the repo with submodules:

```bash
git clone --recurse-submodules https://github.com/mhseals/mhseals_docker.git
```

If you didn't include the recurse-submodules flag, then run the following command to pull each needed submodule:

```
git submodule foreach '
  default_branch=$(git remote show origin | sed -n "/HEAD branch/s/.*: //p")
  echo "Pulling latest changes from $default_branch in $name"
  git fetch origin "$default_branch"
  git checkout "$default_branch"
  git pull origin "$default_branch"
'
```

For your convenience, an environment setup script for each OS has been provided. On Linux, inspect the proposed host changes before approving them:

```bash
./setup.linux.sh --dry-run
./setup.linux.sh
```

The Linux setup detects Debian/Ubuntu, Arch, Fedora, RHEL 8–10 derivatives, rpm-ostree/bootc hosts, and Ubuntu Core. It previews every command and asks before Docker, NVIDIA toolkit, udev, developer-tool, and Compose stages. Use `--components docker,devices,compose` to limit its scope or `--non-interactive --yes` for provisioning. It never installs an NVIDIA driver or recursively changes `/dev`.

On macOS or Windows, run `./setup.mac.sh` or `setup.windows.bat` as before.

Alternatively, you may manually install the following necessary dependencies as you see fit:
- Docker
- NVIDIA Container Toolkit
- Docker Compose
- Node.js LTS and the Dev Container CLI (recommended)
- VS Code with the Dev Containers extension (optional)
- Unity + Vulkan (if you're running the sim, and depending on the available graphics driver)

For manual installation, reference the OS-specific guides below.

### Linux

For manual Docker installation instructions, visit the [Docker Engine installation guide](https://docs.docker.com/engine/install/) and complete the [Linux post-install steps](https://docs.docker.com/engine/install/linux-postinstall/).

The recommended interface is the editor-independent [Dev Container CLI](https://github.com/devcontainers/cli). Install Node.js LTS through your preferred Node version manager or distribution package, then install the CLI:

```bash
node --version
npm install --global @devcontainers/cli
devcontainer --version
```

With Docker running, select the host adapter and start the environment from the repository root:

```bash
.devcontainer/prebuild.sh
devcontainer up --workspace-folder .
devcontainer exec --workspace-folder . bash
```

This workflow works with any host editor. VS Code is optional; opening the repository with its Dev Containers extension invokes the same configuration. To stop and remove the Compose services:

```bash
docker compose \
  -f .devcontainer/docker-compose.yml \
  -f .devcontainer/docker-compose.override.yml \
  down
```

Anything else you would like to install manually, reference the installation scripts for help on figuring out how to install them.

### Container images and local builds

The capability tags are `core`, `cuda`, `jetson`, and `sitl`. `deps` is an internal ARM64 dependency layer used to assemble `jetson`; it is not a host capability. The host probe selects an adapter and writes the ignored `.devcontainer/docker-compose.override.yml`; the devcontainer then pulls the corresponding image. All development services are privileged, and native Linux adapters bind `/dev` for robotics hardware access.

Compose uses the `missing` pull policy by default. This pulls a published image on a clean host while allowing VS Code to start its locally generated `vsc-*-uid` image. To refresh a published image explicitly, run `docker compose pull` before reopening the devcontainer; setting `ASTRO_PULL_POLICY=always` during Dev Containers startup is not supported because Compose would try to pull VS Code's local UID image from Docker Hub.

Image maintainers can opt into a local build without changing the normal Compose file:

```bash
docker compose \
  -f .devcontainer/docker-compose.yml \
  -f .devcontainer/docker-compose.override.yml \
  -f .devcontainer/docker-compose.build.yml \
  build
```

Workspace dependency installation is intentionally excluded from normal container creation. After package manifests change, run `ASTRO_ROSDEP_INSTALL=1 .devcontainer/postcreate.sh` once inside the container.

To diagnose a camera or Cube Orange connection, run `.devcontainer/device-diagnostics.sh`. Scoped host udev rules keep CubePilot, ZED USB, and video devices accessible across reconnects, while passwordless sudo remains available inside the privileged container for targeted recovery. Do not recursively change ownership or permissions beneath `/dev`.

### Image automation

GitHub Actions requires the `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` repository secrets. `core` uses native hosted amd64 and arm64 runners; `cuda` and `sitl` use hosted amd64. JetPack dependencies are built in the generic `deps` image on a native hosted ARM64 runner, so QEMU never executes `apt`, `dpkg`, or ROS compilation. The small `jetson` layer then adds the stable developer user and Python environment.

If a persistent native ARM64 build server becomes available, label its GitHub runner `self-hosted`, `linux`, `ARM64`, and `arm64-builder`, then manually dispatch the image workflow with `arm64_builder` set to `self-hosted`. The normal automatic path continues to use GitHub's hosted ARM64 runner.

Jetson builds publish only the immutable `jetson-<commit>` candidate. The boat does not need to be registered as a GitHub runner. On the boat, validate a candidate directly:

```bash
.devcontainer/boat-validate.sh lunarzdev/astro:jetson-<commit>
```

After the camera, Cube Orange, NVIDIA runtime, ROS, and user checks pass, promote that exact candidate using the Docker credentials already configured on the boat:

```bash
.devcontainer/boat-validate.sh --promote lunarzdev/astro:jetson-<commit>
```

To publish a different moving tag, put it between `--promote` and the candidate image. The compatibility logic for Jazzy on JetPack is pinned to the selected ZED wrapper commit in `Dockerfile.deps`; the repository does not maintain an expanding rosdep skip list.

The source-built ROS foundation is also locked in `ros2-jazzy.lock.repos`. This is necessary because the ROS `jazzy` manifest contains branch names rather than mutually tested commits; resolving those branches during every image build can combine incompatible repository revisions. To prepare a dependency update, regenerate the lock and run its check:

```bash
.devcontainer/lock-repos.py \
  https://raw.githubusercontent.com/ros2/ros2/jazzy/ros2.repos \
  .devcontainer/ros2-jazzy.lock.repos
.devcontainer/check-dependency-lock.py
```

The check intentionally requires review of the `rcutils` compatibility pin. Update that assertion only after the corresponding `rmw` and `rcutils` revisions compile together.

### Windows

For manual installation, get the following programs:

- [VcXsrv (X server for display)](https://sourceforge.net/projects/vcxsrv/)
- [Git](https://git-scm.com/downloads)
- [VSCode](https://code.visualstudio.com/)
- [Docker Desktop](https://docs.docker.com/desktop/release-notes/)

**Start the Docker Daemon each time you want to work on the project by opening the Docker Desktop application.** The first time you install it, you will be prompted to restart your system.

### Mac

> [!IMPORTANT]
> If you are willing to troubleshoot installing a newer version of OpenGL on an X11 Server (XQuartz), follow the steps below and document what you do as much as possible. Otherwise, simply install Linux on your Mac and follow the Linux instructions as normal.

Identify your chip architecture (Intel or Apple Silicon) by running `uname -m`. If your system is an Intel-based Mac, it should output `x86_64`, and if it is Apple Silicon, it will show `arm64`.

Install the following programs through your preferred method:

- [XQuartz (X server for display)](https://www.xquartz.org/)
- [Git](https://git-scm.com/downloads)
- [VSCode](https://code.visualstudio.com/)
- [Docker Desktop](https://docs.docker.com/desktop/release-notes/)

Brew provides an easy way to install all of them at once. Start by installing Brew:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Now, install all of the needed packages:

```bash
brew install git --cask visual-studio-code docker xquartz
```

You will need to restart your system to use both Docker and XQuartz. If, for some reason, you are running a Hackintosh or a macOS VM, it is likely that Docker will complain about Hyper-V for virtualization. Depending on your setup, you will need to add these options: `+vmx,+smep,+smap,+hypervisor` to your VM/boot configuration. You will likely have to troubleshoot issues, but feel free to ask questions here.

After restarting, open XQuartz and enable `File > Preferences > Security > Allow connections from network clients`. **Each time you need to run a GUI application in the Docker container, be sure to run `xhost +` to give XQuartz access to X11 forwarding ports.** For more information, see [X11 Forwarding on macOS and Docker](https://gist.github.com/sorny/969fe55d85c9b0035b0109a31cbcb088). It may be beneficial to add a configuration to your system that runs this command automatically.

## Usage

### Simulation

There are three primary components to the simulation stack:

- Unity physics sim
- Ardupilot SITL control
- ROS navigation logic

For the Unity physics sim, visit [this page](https://github.com/MHSeals/mhseals_asv_sim) and follow the instructions for the setup.

The ROS packages/nodes you run for navigation are all completely up to you depending on what needs to be tested; however, be sure to always use the `ros_tcp_endpoint` package by running `ros2 run ros_tcp_endpoint default_server_endpoint --ros-args -p <arg>:=<value>` (`ROS_IP` and `ROS_TCP_PORT` are useful args for matching the connection with Unity).

ArduPilot SITL is optional rather than part of every devcontainer startup. Start it with the Compose profile, then attach a terminal to `ardupilot_sitl`:

```bash
docker compose \
  -f .devcontainer/docker-compose.yml \
  -f .devcontainer/docker-compose.override.yml \
  --profile sitl up -d sitl
docker exec -it ardupilot_sitl bash
```

After you have access to the terminal, run the following command (it will initially fail unless the Unity connection is already up):

```bash
Tools/autotest/sim_vehicle.py -v "$VEHICLE" $SITL_EXTRA_ARGS
```

and connect it to ROS by starting the MAVROS node

```
ros2 launch mavros apm.launch fcu_url:=tcp://127.0.0.1:5763
```

All of these commands can also be found by running the `help` command in their respective containers.

To run the ROS navigation code, a launch file has been provided for your convenience. Run `ros2 launch mhseals_nav robot.launch.py` with the optional arguments `use_sime_time` (should equal false if running on the actual boat by using `use_sime_time:=false`), `ros_ip` for the Unity simulation IP address, and finally `ros_port` for the simulation port. There are other arguments available for configuring a Zed 2i camera among other things. More details can be found be looking in the `launch` folder.
