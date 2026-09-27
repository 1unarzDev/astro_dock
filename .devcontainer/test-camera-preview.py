import importlib.util
from pathlib import Path
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

spec = importlib.util.spec_from_file_location(
    'preview', Path(__file__).with_name('astro-camera-preview.py'))
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


class PreviewTest(unittest.TestCase):
    def setUp(self):
        self.frames = preview.LatestFrame()
        self.server = preview.preview_server(('127.0.0.1', 0), self.frames)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = 'http://127.0.0.1:{}'.format(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_frame_round_trip_preserves_stamp(self):
        self.frames.put(b'\xff\xd8jpeg\xff\xd9', 123, 456, 'camera_optical')
        self.assertEqual(preview.fetch_frame(self.url),
                         (b'\xff\xd8jpeg\xff\xd9', 123, 456, 'camera_optical'))

    def test_missing_or_stale_frame_is_not_replayed(self):
        with self.assertRaises(HTTPError) as error:
            preview.fetch_frame(self.url)
        self.assertEqual(error.exception.code, 503)
        self.frames.put(b'\xff\xd8jpeg', 1, 2, 'camera')
        self.frames.value = (*self.frames.value[:4], -100)
        with self.assertRaises(HTTPError):
            preview.fetch_frame(self.url)

    def test_reject_wrong_codec_and_oversize(self):
        self.assertFalse(self.frames.put(b'PNG', 1, 0, 'camera'))
        self.assertFalse(self.frames.put(b'\xff\xd8' + b'x' * preview.MAX_FRAME_BYTES,
                                         1, 0, 'camera'))
        self.assertIsNone(self.frames.get())

    def test_page_and_header_sanitization(self):
        self.frames.put(b'\xff\xd8jpeg', 1, 0, 'camera\r\nBad: value')
        self.assertNotIn('\n', preview.fetch_frame(self.url)[3])
        with urlopen(self.url) as response:
            self.assertIn(b'No fresh frame', response.read())


if __name__ == '__main__':
    unittest.main()
