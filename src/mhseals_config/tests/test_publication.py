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

    def test_missing_destination_no_replace(self):
        self.path.unlink()
        spool = publication.prepare(self.path, b'first', None)
        self.assertEqual(publication.publish(spool), 'published')
        self.assertEqual(self.path.read_bytes(), b'first')
        self.assertIsNone(publication.inspect(spool)['displaced'])

    def test_missing_preview_intervening_creation_wins(self):
        self.path.unlink()
        spool = publication.prepare(self.path, b'first', None)
        self.path.write_bytes(b'editor created')
        with self.assertRaises(FileExistsError):
            publication.publish(spool)
        self.assertEqual(self.path.read_bytes(), b'editor created')

    def test_symlink_and_fifo_refused_without_touching_target(self):
        target = self.path.with_suffix('.target')
        target.write_bytes(b'protected')
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            publication.publish(self.spool)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_bytes(), b'protected')
        self.path.unlink()
        os.mkfifo(self.path)
        spool = publication.prepare(self.path, b'new', self.expected)
        with self.assertRaises(ValueError):
            publication.publish(spool)
        self.assertTrue(self.path.exists())

    def test_symlink_parent_refused(self):
        alias = Path(self.temp.name) / 'alias'
        alias.symlink_to(self.temp.name, target_is_directory=True)
        with self.assertRaises(OSError):
            publication.prepare(alias / 'hardware.yaml', b'new', self.expected)

    def test_write_to_displaced_fd_during_publication_is_conflict(self):
        move = publication._move
        with self.path.open('r+b') as editor:
            def race(source_fd, source, target_fd, target):
                if source == 'candidate':
                    editor.write(b'edited')
                    editor.flush()
                move(source_fd, source, target_fd, target)
            with patch.object(publication, '_move', side_effect=race):
                self.assertEqual(publication.publish(self.spool), 'conflict_displaced')
        self.assertEqual((self.spool / 'displaced').read_bytes(), b'edited')
        self.assertTrue(publication.inspect(self.spool)['source_changed'])

    def test_displaced_slot_collision_never_replaces_retained_data(self):
        (self.spool / 'displaced').write_bytes(b'retained')
        with self.assertRaises(FileExistsError):
            publication.publish(self.spool)
        self.assertEqual(self.path.read_bytes(), b'old')
        self.assertEqual((self.spool / 'displaced').read_bytes(), b'retained')

    def test_inspection_reports_subsequent_destination_change(self):
        publication.publish(self.spool)
        self.path.write_bytes(b'later')
        self.assertTrue(publication.inspect(self.spool)['destination_changed'])
        self.assertFalse(publication.inspect(self.spool)['source_changed'])
