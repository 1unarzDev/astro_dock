# Camera preview between Foxy and Jazzy

Squirtle's JetPack 5 GPU stack needs legacy Foxy/ZED 4. It cannot directly join
the Jazzy boat domain: a hardware test produced DDS deserialization errors and
`std::bad_alloc` in both Foxy camera processes when the Jazzy subscriber joined.
This is independent of GPU/camera permissions. Keep Foxy on domain **142** and
Jazzy on **42**. JetPack 6/Jazzy does not need this cross-version workaround.

The shared `astro-camera-preview` command moves JPEG bytes and camera timestamps
over HTTP instead of exposing either distribution's DDS to the other. It works
in both image generations and core; from an older image use
`python3 .devcontainer/astro-camera-preview.py` in the sourced workspace.

## Jetson

In two container shells (the container already uses host networking):

```sh
ros2 launch zed_wrapper zed_camera.launch.py camera_model:=zed2i
astro-camera-preview serve --bind 192.168.0.153
```

Default input is `/zed/zed_node/left/image_rect_color/compressed`. Use `--topic`
for another camera name or SDK topic layout. `compressed_image_transport` must
be installed; both Jetson package lists include it. For an older running image:
`sudo -E astro-ros-install compressed_image_transport`.

View **http://squirtle-jetson.local:8088** in any laptop browser. The page hides
stale frames instead of implying a disconnected camera is still live.

The HTTP endpoint is **unauthenticated and unencrypted**, intended only for the
trusted boat LAN. It binds loopback unless `--bind` is explicitly supplied. Do
not expose it to the Internet. On untrusted networks keep the loopback default
and use `ssh -L 8088:127.0.0.1:8088 roboboat@squirtle-jetson.local`.

## Jazzy laptop or Odroid

In a sourced Jazzy container/shell on its normal domain 42:

```sh
astro-camera-preview receive http://squirtle-jetson.local:8088
```

This publishes `/astro_camera/image/compressed` as standard
`sensor_msgs/msg/CompressedImage`. In RViz's Image display select the
`/astro_camera/image` topic and `compressed` transport. If a tool needs raw
images, run the standard image transport decoder in another Jazzy shell:

```sh
ros2 run image_transport republish compressed raw --ros-args \
  -r in/compressed:=/astro_camera/image/compressed \
  -r out:=/astro_camera/image
```

The receiver defaults to 5 FPS (`--fps` changes it) and preserves the original
camera stamp/frame ID. Hosts must have synchronized clocks. Duplicate timestamps
are not republished; a camera silent for two seconds returns HTTP 503. There is
no recording/backlog and network failures retry. Stop with Ctrl-C.

This is a **lossy debugging preview**, not a calibrated navigation sensor:
CameraInfo, depth, point clouds, TF and custom ZED messages are not bridged.
Avoid feeding the preview into localization or metric vision without designing
and validating the required calibration, transport and latency contracts.
