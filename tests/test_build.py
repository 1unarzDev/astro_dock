"""hw.sh build (devices/build.sh) and hw.sh image (.devcontainer/dev-image.sh) with stand-ins: a colcon that
records its arguments, a docker that answers from fixtures. Nothing is built or pulled."""
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COLCON = '#!/bin/sh\necho "colcon $*" >> "$COLCON_LOG"; mkdir -p build/pkg install\n'
# docker: compose config names the tag; images lists ids; image inspect answers from $DOCKER_DATA/<ref>
DOCKER = r'''#!/usr/bin/env bash
case "$1 $2" in
  "compose -f") echo "$DEV_TAG"; exit 0 ;;
  "images -q") ls "$DOCKER_DATA/ids"; exit 0 ;;
esac
[[ $1 == image && $2 == inspect ]] || exit 1
shift 2; fmt=''
[[ $1 == --format ]] && { fmt=$2; shift 2; }
rc=0
for ref; do
  f=$DOCKER_DATA/ids/${ref//[\/:]/_}; [[ -f $f ]] || { rc=1; continue; }
  read -r id created layers name < "$f"
  case $fmt in
    *RootFS.Layers*'}}') [[ $fmt == '{{.Id}}'* ]] && echo "$id $created $layers" || echo "$layers" ;;
    *RepoTags*) echo "$name" ;;
    *) : ;;
  esac
done
exit $rc
'''


def run_body(ws, log, **env):
    body = subprocess.run(['bash', '-c', f'source {ROOT}/devices/build.sh; hw_build_body'],
                          capture_output=True, text=True, check=True).stdout
    return subprocess.run(['bash', '-s', '--', '--packages-select', 'pkg'], input=body, capture_output=True,
                          text=True, env={**os.environ, 'PATH': f'{log.parent}:{os.environ["PATH"]}',
                                          'COLCON_LOG': str(log), 'ASTRO_BUILD_WS': str(ws), **env})


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / 'colcon').write_text(COLCON)
        (tmp / 'colcon').chmod(0o755)
        self.log = tmp / 'colcon.log'
        self.ws = tmp / 'ws'
        (self.ws / '.devcontainer').mkdir(parents=True)
        (self.ws / '.devcontainer/dev.bashrc').write_text('')

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_stamp_cleans_only_when_the_image_or_defaults_change(self):
        first = run_body(self.ws, self.log, ASTRO_BUILD_IMAGE='sha256:a', ASTRO_BUILD_DEFAULTS='build: {}')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual((self.ws / 'colcon_defaults.yaml').read_text(), 'build: {}\n')   # a ws without one
        self.assertIn('colcon build --symlink-install --packages-select pkg', self.log.read_text())
        (self.ws / 'build/pkg/CMakeCache.txt').write_text('old')
        same = run_body(self.ws, self.log, ASTRO_BUILD_IMAGE='sha256:a')
        self.assertNotIn('from clean', same.stdout)
        self.assertTrue((self.ws / 'build/pkg/CMakeCache.txt').exists())
        pulled = run_body(self.ws, self.log, ASTRO_BUILD_IMAGE='sha256:b')
        self.assertIn('building from clean', pulled.stdout)
        self.assertFalse((self.ws / 'build/pkg/CMakeCache.txt').exists())
        self.assertIn('image sha256:b', (self.ws / '.astro-build').read_text())

    def test_from_clean_builds_the_whole_workspace_not_the_selection(self):
        # hot's --packages-select after colcon_defaults.yaml changed: a clean must not leave the others uninstalled
        run_body(self.ws, self.log, ASTRO_BUILD_IMAGE='sha256:a')
        (self.ws / 'colcon_defaults.yaml').write_text('build: {parallel-workers: 1}\n')
        self.log.write_text('')
        stale = run_body(self.ws, self.log, ASTRO_BUILD_IMAGE='sha256:a')
        self.assertEqual(stale.returncode, 0, stale.stderr)
        self.assertIn('building the whole workspace, not only: --packages-select pkg', stale.stdout)
        self.assertEqual(self.log.read_text().strip(), 'colcon build --symlink-install')

    def test_no_image_never_cleans_and_unstamped_cleans_only_when_asked(self):
        (self.ws / 'build/pkg').mkdir(parents=True)
        (self.ws / 'build/pkg/keep').write_text('')
        run_body(self.ws, self.log)
        self.assertTrue((self.ws / 'build/pkg/keep').exists())
        self.assertFalse((self.ws / '.astro-build').exists())              # no image: no stamp
        run_body(self.ws, self.log, ASTRO_BUILD_IMAGE='sha256:a')            # the boat's first stamped build
        self.assertTrue((self.ws / 'build/pkg/keep').exists())
        (self.ws / '.astro-build').unlink()
        run_body(self.ws, self.log, ASTRO_BUILD_IMAGE='sha256:a', ASTRO_BUILD_CLEAN_UNSTAMPED='1')
        self.assertFalse((self.ws / 'build/pkg/keep').exists())

    def test_a_build_on_underlay_files_gone_builds_from_clean(self):
        # 11-04: a newer image left build.make on /opt/ros/<distro>/lib/libfastcdr.so.2.2.7, with no stamp to see it
        underlay = self.ws.parent / 'underlay'
        (underlay / 'lib').mkdir(parents=True)
        lib = underlay / 'lib/libfastcdr.so.2.2.7'
        lib.write_text('')
        made = self.ws / 'build/pkg/CMakeFiles/pkg.dir/build.make'
        made.parent.mkdir(parents=True)
        made.write_text(f'libpkg.so: {lib}\nlibpkg.so: {self.ws}/src/gone.cpp\n')
        env = dict(AMENT_PREFIX_PATH=f'{self.ws}/install/pkg:{underlay}')
        kept = run_body(self.ws, self.log, **env)
        self.assertNotIn('from clean', kept.stdout)      # its own sources never count
        self.assertTrue(made.exists())
        lib.unlink()
        gone = run_body(self.ws, self.log, **env)
        self.assertIn(f'depends on {lib}', gone.stdout)
        self.assertFalse(made.exists())

    def test_a_failed_colcon_fails_the_build_and_writes_no_stamp(self):
        (self.log.parent / 'colcon').write_text('#!/bin/sh\nexit 3\n')
        done = run_body(self.ws, self.log, ASTRO_BUILD_IMAGE='sha256:a')
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse((self.ws / '.astro-build').exists())

    def test_the_workspace_has_one_build(self):
        # hosts apply, hot and setup's repos stage run this body; no script runs colcon build itself
        # (log, echo and help text naming it is fine). hw.sh overlay builds a worktree overlay, not the workspace.
        scripts = [ROOT / 'hw.sh', ROOT / 'setup.linux.sh', *(ROOT / 'devices').rglob('*.sh')]
        for path in scripts:
            if path.name in ('build.sh', 'overlay.sh'):
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                code = line.split('#', 1)[0]
                if 'colcon build' in code and not re.search(r'\b(log|echo|die)\b|^\s*"', code):
                    self.fail(f'{path.relative_to(ROOT)}:{n} runs colcon build outside devices/build.sh')
        for path in ('devices/field.sh', 'devices/hosts/fleet.sh'):
            self.assertIn('hw_build_body', (ROOT / path).read_text())
        self.assertIn('hw.sh" build', (ROOT / 'setup.linux.sh').read_text())


class ImageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / 'docker').write_text(DOCKER)
        (tmp / 'docker').chmod(0o755)
        self.ids = tmp / 'data/ids'
        self.ids.mkdir(parents=True)
        self.dc = tmp / 'dc'
        self.dc.mkdir()
        for name in ('docker-compose.yml', 'docker-compose.override.yml'):
            (self.dc / name).write_text('services: {dev: {image: reg/img:tag}}\n')
        (self.dc / 'dev-image.sh').write_text((ROOT / '.devcontainer/dev-image.sh').read_text())
        self.env = {**os.environ, 'PATH': f'{tmp}:{os.environ["PATH"]}', 'DOCKER_DATA': str(tmp / 'data'),
                    'DEV_TAG': 'reg/img:tag'}

    def tearDown(self):
        self.tmp.cleanup()

    def image(self, name, created, layers, tag):
        (self.ids / name).write_text(f'{name} {created} {layers} {tag}\n')

    def resolve(self, *args):
        return subprocess.run(['bash', str(self.dc / 'dev-image.sh'), *args], capture_output=True, text=True,
                              env=self.env, timeout=30)

    def test_local_prefers_the_variant_built_on_the_current_tag(self):
        self.image('reg_img_tag', '2026-11-02', '["l1","l2"]', 'reg/img:tag')
        self.image('uidold', '2026-10-01', '["l0","lu"]', 'vsc-x-old-uid:latest')         # built on an older tag
        self.image('uidnew', '2026-11-03', '["l1","l2","lu"]', 'vsc-x-new-uid:latest')
        self.assertEqual(self.resolve().stdout.strip(), 'reg/img:tag')
        self.assertEqual(self.resolve('--local').stdout.strip(), 'vsc-x-new-uid:latest')
        (self.ids / 'uidnew').unlink()
        self.assertEqual(self.resolve('--local').stdout.strip(), 'reg/img:tag')       # a stale variant: the tag

    def test_pull_checks_with_docker_pull_when_no_digest_is_read(self):
        # no buildx here (the stand-in refuses it): docker pull checks, and its failure keeps the local image
        self.image('reg_img_tag', '2026-11-02', '["l1"]', 'reg/img:tag')
        done = self.resolve('--pull')
        self.assertEqual((done.returncode, done.stdout.strip()), (0, 'reg/img:tag'), done.stderr)
        self.assertIn('with docker pull', done.stderr)
        self.assertIn('keeping the local image', done.stderr)
        quiet = subprocess.run(['bash', str(self.dc / 'dev-image.sh'), '--pull'], capture_output=True, text=True,
                               env={**self.env, 'ASTRO_NO_PULL': '1'}, timeout=30)
        self.assertEqual(quiet.stderr, '')

    def test_missing_pieces_name_their_fix(self):
        missing = self.resolve('--local')
        self.assertEqual(missing.returncode, 2)
        self.assertIn('prebuild.sh', missing.stderr)
        (self.dc / 'docker-compose.override.yml').unlink()
        self.assertIn('run ', self.resolve().stderr)


if __name__ == '__main__':
    unittest.main()
