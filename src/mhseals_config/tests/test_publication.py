"""Offline publication races use only per-test temporary files."""
import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from mhseals_config import _publication as publication


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'hardware.yaml'
        self.path.write_bytes(b'old')
        self.expected = publication.observe(self.path)
        self.spool = Path(publication.prepare(self.path, b'new', self.expected))

    def test_publish_retains_separate_inodes(self):
        self.assertEqual(publication.publish(self.spool), 'published')
        self.assertEqual(self.path.read_bytes(), b'new')
        self.assertEqual((self.spool / 'displaced').read_bytes(), b'old')
        self.assertNotEqual(self.path.stat().st_ino, (self.spool / 'snapshot').stat().st_ino)
        self.assertFalse(publication.inspect(self.spool)['source_changed'])
        with self.assertRaises(ValueError):
            publication.publish(self.spool)

    def test_editor_save_after_check_before_move(self):
        move = publication._move
        def race(source_fd, source, target_fd, target):
            if target == 'displaced':
                editor = self.path.with_suffix('.editor')
                editor.write_bytes(b'editor')
                editor.replace(self.path)
            move(source_fd, source, target_fd, target)
        with patch.object(publication, '_move', side_effect=race):
            self.assertEqual(publication.publish(self.spool), 'conflict_displaced')
        self.assertEqual((self.spool / 'displaced').read_bytes(), b'editor')
        self.assertEqual((self.spool / 'candidate').read_bytes(), b'new')

    def test_editor_recreates_name_before_publish(self):
        move = publication._move
        def race(source_fd, source, target_fd, target):
            if source == 'candidate':
                self.path.write_bytes(b'editor')
            move(source_fd, source, target_fd, target)
        with patch.object(publication, '_move', side_effect=race):
            with self.assertRaises(FileExistsError):
                publication.publish(self.spool)
        self.assertEqual(self.path.read_bytes(), b'editor')
        self.assertEqual((self.spool / 'displaced').read_bytes(), b'old')

    def test_late_descriptor_write_remains_inspectable(self):
        with self.path.open('r+b') as editor:
            publication.publish(self.spool)
            editor.write(b'late editor')
            editor.flush()
            os.fsync(editor.fileno())
        self.assertTrue(publication.inspect(self.spool)['source_changed'])
        self.assertEqual((self.spool / 'displaced').read_bytes(), b'late editor')
        self.assertEqual((self.spool / 'snapshot').read_bytes(), b'new')
        self.assertEqual(self.path.read_bytes(), b'new')

    def test_unsupported_move_preserves_all_bytes(self):
        with patch.object(publication, '_move', side_effect=OSError(errno.ENOTSUP, 'unsupported')):
            with self.assertRaises(OSError):
                publication.publish(self.spool)
        self.assertEqual(self.path.read_bytes(), b'old')
        self.assertEqual((self.spool / 'candidate').read_bytes(), b'new')
