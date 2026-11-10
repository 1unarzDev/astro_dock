# Docker Desktop settings astro_dock needs on Windows (setup.windows.bat): host networking, so network_mode: host
# reaches the Windows host's ports (crane_sim's ROS TCP endpoint on 10000; Docker Desktop 4.34+, off by default).
# Prints "changed" when it wrote the file (Docker Desktop must restart to apply it).
$s = Join-Path $env:APPDATA "Docker\settings-store.json"
$j = if (Test-Path $s) { Get-Content $s -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
if ($j.HostNetworkingEnabled -ne $true) {
  New-Item -ItemType Directory -Force (Split-Path $s) | Out-Null
  $j | Add-Member -NotePropertyName HostNetworkingEnabled -NotePropertyValue $true -Force
  $j | ConvertTo-Json -Depth 20 | Set-Content $s -Encoding utf8
  "changed"
}
