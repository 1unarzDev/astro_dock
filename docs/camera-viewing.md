# Camera viewing

Run the camera in its Jetson container:

```sh
ros2 launch zed_wrapper zed_camera.launch.py camera_model:=zed2i
```

For legacy JetPack 5/Foxy, run `ros2 launch rosbridge_server rosbridge_websocket_launch.xml`.
In Foxglove, choose **Rosbridge**, connect to `ws://squirtle-jetson.local:9090`,
and select `/zed/zed_node/left/image_rect_color/compressed` in an Image panel.

For JetPack 6/Jazzy, run `ros2 launch foxglove_bridge foxglove_bridge_launch.xml`.
Choose **Foxglove WebSocket** and `ws://<host>:8765`.

Keep these unauthenticated endpoints on the trusted boat LAN. Foxy remains on
DDS domain 142; Jazzy remains on 42. Jazzy RViz cannot directly consume Foxy's
topics safely; use Foxglove, or a matching Foxy RViz environment.
