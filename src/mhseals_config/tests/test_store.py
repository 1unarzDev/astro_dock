from pathlib import Path
import tempfile
import unittest
from mhseals_config._store import Store


class StoreTests(unittest.TestCase):
    def test_no_follow_components_and_objects(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as external:
            (Path(root) / 'linked').symlink_to(external)
            with self.assertRaises(OSError):
                Store(root, 'linked')
            with Store(root, 's') as store:
                store.mkdir('initializing')
                store.write('initializing/a.json', {'a': 1})
                self.assertEqual(store.read('initializing/a.json'), {'a': 1})
                directory = Path(root) / 's'
                (directory / 'active.json').symlink_to(Path(external) / 'missing')
                with self.assertRaises(ValueError):
                    store.exists('active.json')
                with self.assertRaises(OSError):
                    store.read('active.json')
                with self.assertRaises(ValueError):
                    store.read('../outside')
                (directory / 'redirect').symlink_to(external)
                with self.assertRaises(OSError):
                    store.write('redirect/a.json', {})
            self.assertEqual(list(Path(external).iterdir()), [])
