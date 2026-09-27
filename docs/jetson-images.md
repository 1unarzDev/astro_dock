# Jetson images and package installation

| Runtime tag | Host | Container stack | Role |
| --- | --- | --- | --- |
| `jetson-jp5-foxy` | JetPack 5 / L4T R35 (Xavier NX) | Ubuntu 20.04, ROS Foxy, ZED 4.0 | Legacy camera/GPU |
| `jetson-jp6-jazzy` | JetPack 6 / L4T R36 (Orin) | Ubuntu 22.04, source-built Jazzy, ZED 5.4 | Current camera/GPU |
| `core` | ODROID / ordinary ARM64 or AMD64 Linux | Ubuntu 24.04, Jazzy | MAVROS, navigation, ODROID PWM |

The original Jetson Nano on JetPack 4/R32 is **not** supported by either
Jetson image. Squirtle reports Xavier NX, R35.4.1. Foxy is end-of-life: the
legacy image is an explicitly isolated compatibility solution, not a supported
modern ROS platform. Never install Noble/Jazzy apt repositories on Focal/Jammy.
The old ambiguous `jetson` registry tag is historical and is no longer selected.

Both Jetson Dockerfiles share `jetson-user.sh`, `configure-zed-access.sh`,
`astro-ros-install.sh`, operator help, and deployment/runtime-validation scripts.
Only the platform/ROS/SDK dependency build differs. Package caches and ZED
settings/resources are separated by JetPack generation. Neither image includes
MAVROS; the `core` smoke tests require MAVROS, Extras and the geoid dataset on
both architectures.

## Deploy

Build/pull the explicitly named image. CI publishes `*-next` candidates;
hardware validation is required before treating a candidate as stable.

```sh
docker pull lunarzdev/astro:jetson-jp5-foxy-next
.devcontainer/deploy-jetson.sh lunarzdev/astro:jetson-jp5-foxy-next "$PWD"
.devcontainer/validate-jetson-runtime.sh roboboat_dev
docker exec -it roboboat_dev bash
```

Use the `jetson-jp6-jazzy` family on R36 hosts. The common deploy script checks
the image's JetPack label and architecture, preserves the host hostname, maps
video/render GIDs, and refuses to replace an existing `roboboat_dev` container.
Inspect existing work before explicitly replacing it. Compose/devcontainer
users can run `prebuild.sh` to select the host generation automatically.

For an isolated boat network, pull on an internet-connected computer using
`docker pull --platform linux/arm64 IMAGE`, then stream `docker save IMAGE`
over SSH into `docker load` on the Jetson. Deployment reuses an already-loaded
image without contacting the registry. This avoids changing the boat's routes.

Validation requires CUDA initialization as `roboboat`, accessible camera
devices and at least three nonempty ROS image messages. Merely seeing a camera
node or ROS topic is not a pass. It starts a temporary ZED publisher and stops
only that publisher afterward. For permanent camera operation use the launch
command in container `help`. No thruster command is involved.

Foxy and Jazzy custom ZED interfaces differ. Standard Image messages may
interoperate over DDS, but validate delivery on the actual peer before relying
on it; matching domain IDs alone do not prove compatibility.

## Install packages on either image

```sh
sudo -E astro-ros-install topic_tools
# Force the source resolver for a package without an installed underlay copy:
sudo -E astro-ros-install --source examples_rclcpp_minimal_publisher
```

The shared command validates package names, serializes installations, sources
the existing overlays, uses distro debs when available, and otherwise uses
`rosinstall_generator` + `vcstool` + `rosdep` + `colcon` to resolve/build only
missing ROS dependencies. It dynamically excludes installed underlay packages;
there is no handwritten, growing list of ignored dependencies.

Source installs go to `/opt/astro-extra`. Open a fresh shell after installation.
Resolved exact source manifests are recorded in `/opt/astro-extra/manifests`.
Failures leave their temporary workspace and logs for diagnosis; incompatible
source code or unavailable native dependencies still require attention. This
is dependency automation, not a guarantee that every ROS release supports every
Ubuntu/SDK combination.

To persist additions, put ROS package names (one per line) in
`.devcontainer/jetson-jp5-packages.txt` or `jetson-jp6-packages.txt` and rebuild.
Both use the same installer. Keep platform-specific lists separate because
package availability differs. Ad-hoc container installs disappear on recreation;
the source revision receipt helps diagnose changes when rebuilding later.

## Build failure audit

The September 26 failures were CUDA smoke tests sourcing the Jetson-only
`/opt/astro-setup.bash`, not failures of the Jetson source build. CUDA wrapper
dependency errors were also masked by a non-fail-fast shell and uninitialized
rosdep. Smoke expectations now differ by role; wrapper builds initialize rosdep
and stop on failure. ROS setup scripts cannot safely be sourced with `set -u`,
so these build steps use `set -e` without nounset.
