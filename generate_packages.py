import shutil
import os
import ast
import xml.etree.ElementTree as ET
from pathlib import Path

# Load ignored packages
ignore_file = Path(".devcontainer/package-ignore.txt")
ignored_packages = set()
if ignore_file.exists():
    with open(ignore_file, "r") as f:
        ignored_packages = {line.strip() for line in f if line.strip()}

# Helper function to read existing dependencies from a file
def read_existing(file_path):
    if file_path.exists():
        with open(file_path, "r") as f:
            return {line.strip() for line in f if line.strip()}
    return set()

# Helper function to append missing dependencies to a file
def append_to_file(file_path, packages):
    existing = read_existing(file_path)
    new_packages = packages - existing
    if not file_path.exists():
        file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "a") as f:
        for pkg in sorted(new_packages):
            f.write(pkg + "\n")
    return len(new_packages)

# Get pip dependencies listed in packages
workspace = Path("src")
requirements = set()

for setup_file in workspace.rglob("setup.py"):
    with open(setup_file, "r") as f:
        source = f.read()

    try:
        tree = ast.parse(source)
    except SyntaxError:
        print(f"Skipping {setup_file}: syntax error")
        continue

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "setup":
            for kw in node.keywords:
                if kw.arg == "install_requires" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    for elt in kw.value.elts:
                        if isinstance(elt, ast.Constant):
                            requirements.add(str(elt.value))
                        elif hasattr(ast, "Str") and isinstance(elt, ast.Str):
                            requirements.add(elt.s)

req_file = Path("requirements.txt")
if requirements:
    added_count = append_to_file(req_file, requirements)
    print(f"✅ Updated {req_file} with {added_count} new packages.")
else:
    print("⚠️ No install_requires found.")

# Get apt dependencies listed in packages
apt_packages = set()
local_packages = {p.name for p in workspace.iterdir() if p.is_dir()}

for package_xml in workspace.rglob("package.xml"):
    if "install" in package_xml.parts:
        continue

    try:
        root = ET.parse(package_xml).getroot()
    except ET.ParseError:
        print(f"Skipping {package_xml}: XML parse error")
        continue

    for dep_tag in ["depend", "build_depend", "exec_depend", "run_depend"]:
        for dep in root.findall(dep_tag):
            if dep.text:
                dep_name = dep.text.strip()
                if dep_name in ignored_packages:
                    # Skip ignored packages
                    print(f"Skipping ignored package: {dep_name}")
                    continue
                if dep_name in local_packages:
                    resp = input(f"⚠️  '{dep_name}' appears to be a local package. Remove from apt list? [y/N] ")
                    if resp.lower() == "y":
                        continue
                apt_packages.add(dep_name)

ros_distro = os.environ.get("ROS_DISTRO", "jazzy")
apt_packages = {f"ros-{ros_distro}-{p.replace('_', '-')}" for p in apt_packages}
apt_file = Path("apt-packages.txt")
if apt_packages:
    added_count = append_to_file(apt_file, apt_packages)
    print(f"✅ Updated {apt_file} with {added_count} new packages.")
else:
    print("⚠️ No apt dependencies found.")

# Optionally move files to .devcontainer
move = input("❓ Would you like to move the files to .devcontainer? [y/N] ")
if move.lower() == 'y':
    try:
        devcontainer_dir = Path(".devcontainer")
        devcontainer_dir.mkdir(exist_ok=True)
        shutil.move(req_file, devcontainer_dir / req_file.name)
        print(f"✅ Moved '{req_file}' to '{devcontainer_dir}'")
        shutil.move(apt_file, devcontainer_dir / apt_file.name)
        print(f"✅ Moved '{apt_file}' to '{devcontainer_dir}'")
    except Exception as e:
        print(f"❌ A file transfer error occurred: {e}")

print("🏁 Package generation complete!")
