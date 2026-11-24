"""astro's field log (tools/astro/field.py): checklist parsing, failure signatures, auto-issue dedup and cap,
and the files sync copies. gh is a fake script on PATH; nothing reaches the network."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
os.environ['ASTRO_FIELD'] = 'off'          # never file from tests; the gh tests turn it on around a fake gh

from astro import field  # noqa: E402

FAKE_GH = '''#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
case "$1 $2" in
  "issue list") cat "$GH_OPEN" ;;
  "issue create") cat > /dev/null; echo "https://example.invalid/o/r/issues/7" ;;
esac
'''


class Vessel:
    name = 'testboat'

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.astro = {'field': {'repo': 'o/r', 'dir': str(self.tmp / 'field')}}

    def data_root(self):
        return self.tmp / 'data'


class Checklist(unittest.TestCase):
    def test_items_checks_and_sections(self):
        text = '\n'.join([
            '# Pre-flight', '## Dock',
            '1. Update the code:', '   ```', '   1. not an item', '   - nor this', '   ```',
            '   - a nested note',
            '- [ ] sensors up — check: `astro ps sensors`',
            '- [x] RC on, mode GUIDED',
            '## Water', '* disk free - check: `df -h | tail -n1`'])
        items = field.parse_checklist(text)
        self.assertEqual([(i['section'], i['text'], i['check']) for i in items], [
            ('Dock', 'Update the code:', None),
            ('Dock', 'sensors up', 'astro ps sensors'),
            ('Dock', 'RC on, mode GUIDED', None),
            ('Water', 'disk free', 'df -h | tail -n1')])

    def test_a_check_shows_ok_or_fail_with_one_line(self):
        self.assertEqual(field.run_check('echo a; echo up 3 nodes'), (True, 'up 3 nodes'))
        self.assertEqual(field.run_check('echo partial; echo no route >&2; exit 2'), (False, 'no route'))


class AutoIssues(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / 'bin').mkdir()
        (tmp / 'bin/gh').write_text(FAKE_GH)
        (tmp / 'bin/gh').chmod(0o755)
        self.log, self.open = tmp / 'gh.log', tmp / 'open.json'
        self.open.write_text('[]')
        self.env = mock.patch.dict(os.environ, {'ASTRO_FIELD': 'on', 'GH_LOG': str(self.log), 'GH_OPEN': str(self.open),
                                                'PATH': f'{tmp / "bin"}{os.pathsep}{os.environ["PATH"]}'})
        self.env.start()
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.field = field.Field(Vessel(tmp))

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        self.env.stop()
        self.tmp.cleanup()

    def calls(self, verb):
        return [c for c in (self.log.read_text().splitlines() if self.log.exists() else []) if c.startswith(verb)]

    def test_signature_ignores_numbers_and_hashes_only(self):
        a = field.signature('test', 'couple-surge', 'no odometry after 5.0 s (state 3f2a91)\ndetail')
        self.assertEqual(a, field.signature('test', 'couple-surge', 'No odometry after 12 s (state 0b77c4)'))
        self.assertNotEqual(a, field.signature('test', 'couple-sway', 'no odometry after 5.0 s'))
        self.assertNotEqual(a, field.signature('stack', 'couple-surge', 'no odometry after 5.0 s'))

    def test_a_repeat_comments_on_the_open_issue_at_most_hourly(self):
        self.assertEqual(self.field.auto('stack', 'drive', 'not ready in 60 s'), 'filed')
        sig = field.signature('stack', 'drive', 'not ready in 60 s')
        self.open.write_text(f'[{{"number": 7, "body": "x <!-- astro-signature: {sig} -->"}}]')
        self.assertEqual(self.field.auto('stack', 'drive', 'not ready in 61 s'), 'comment')
        self.assertEqual(self.field.auto('stack', 'drive', 'not ready in 60 s'), 'comment')
        self.assertEqual(len(self.calls('issue create')), 1)
        self.assertEqual(len(self.calls('issue comment 7')), 1)           # the second repeat stays quiet

    def test_at_most_five_new_auto_issues_a_day(self):
        got = [self.field.auto('test', f'test-{chr(97 + i)}', 'aborted: lane taken') for i in range(7)]
        self.assertEqual(got, ['filed'] * 5 + ['cap'] * 2)
        self.assertEqual(len(self.calls('issue create')), 5)
        self.assertIn('--label auto', self.calls('issue create')[0])

    def test_offline_queues_once_per_signature_and_sync_files_it(self):
        with mock.patch.dict(os.environ, {'PATH': '/nonexistent'}):          # no gh: offline
            self.assertEqual(self.field.auto('apply', 'speed-surge', 'check refused: d1 < 0'), 'queued')
            self.assertEqual(self.field.auto('apply', 'speed-surge', 'check refused: d1 < 0'), 'queued')
        queued = yaml.safe_load(self.field.pending_path().read_text())['issues']
        self.assertEqual(len(queued), 1)
        self.assertIn('astro-signature', queued[0]['body'])
        self.assertEqual(self.field.flush(), (1, 0))
        self.assertFalse(self.field.pending_path().exists())

    def test_off_switch_never_calls_gh(self):
        with mock.patch.dict(os.environ, {'ASTRO_FIELD': 'off'}):
            self.assertIsNone(field.auto(self.field.vessel, 'test', 'still', 'no odometry'))
        self.assertEqual(self.calls(''), [])


class Sync(unittest.TestCase):
    def test_copies_log_results_without_samples_and_a_bag_index(self):
        import datetime
        with tempfile.TemporaryDirectory() as tmp:
            f = field.Field(Vessel(tmp))
            day = datetime.date(2030, 1, 2)
            src = Path(tmp, 'data', day.isoformat())
            (src / 'results').mkdir(parents=True)
            (src / 'bags/120000-lake').mkdir(parents=True)
            (src / 'bags/120000-lake/x.mcap').write_bytes(b'0' * 2048)
            (src / 'stacks').mkdir()
            (src / 'stacks/drive.log').write_text('noise')
            (src / 'bundle-120500.tgz').write_bytes(b'tgz')
            (src / '.rec.pid').write_text('1 x')
            (src / 'log.txt').write_text('12:00:01  rec     start bags/120000-lake (lean)\n')
            result = {'test': 'speed-surge', 'summary': 'd1=0.4', 'measured': {'holds': [{'level': 0.3}]},
                      'holds': [{'command': {'axis': 'surge'}, 'start': 1.0, 'samples': {'t': [0.0, 0.05]}}]}
            (src / 'results/speed-surge-120300.yaml').write_text(yaml.safe_dump(result))
            session = f.session(day)
            self.assertEqual(session.name, '2030-01-02-lake')                # the day's first rec label
            self.assertEqual(f.collect(day, session), ['log.txt', 'results/speed-surge-120300.yaml', 'bags.txt'])
            self.assertEqual(sorted(p.name for p in session.iterdir()), ['bags.txt', 'log.txt', 'results'])
            kept = yaml.safe_load((session / 'results/speed-surge-120300.yaml').read_text())
            self.assertEqual(kept['holds'], [{'command': {'axis': 'surge'}, 'start': 1.0}])
            self.assertEqual((kept['summary'], kept['measured']), ('d1=0.4', {'holds': [{'level': 0.3}]}))
            self.assertRegex((session / 'bags.txt').read_text(), r'^120000-lake  \S+:\S+120000-lake  \(recording\)  2\.0K')
            self.assertEqual(f.collect(day, session), ['log.txt', 'results/speed-surge-120300.yaml', 'bags.txt'])


if __name__ == '__main__':
    unittest.main()
