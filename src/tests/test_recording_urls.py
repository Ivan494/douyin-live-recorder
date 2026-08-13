import unittest
from types import SimpleNamespace

from recording_urls import (
    ffmpeg_live_input_options,
    has_recording_url,
    recording_extension,
    recording_input_url,
)


class RecordingUrlSelectionTest(unittest.TestCase):
    def test_prefers_direct_flv_over_hls_record_url(self):
        stream = SimpleNamespace(
            flv_url="http://cdn.example/live.flv?sign=flv",
            m3u8_url="http://cdn.example/live.m3u8?sign=hls",
            record_url="http://cdn.example/live.m3u8?sign=hls",
        )

        url, kind = recording_input_url(stream)

        self.assertEqual("http://cdn.example/live.flv?sign=flv", url)
        self.assertEqual("flv", kind)
        self.assertEqual("mp4", recording_extension(url, "mp4"))
        self.assertTrue(has_recording_url(stream))

    def test_live_http_input_options_enable_reconnect_without_eof_loop(self):
        options = ffmpeg_live_input_options("https://cdn.example/live.flv?sign=1")

        self.assertEqual("30000000", options[options.index("-rw_timeout") + 1])
        self.assertEqual("1", options[options.index("-reconnect") + 1])
        self.assertEqual("1", options[options.index("-reconnect_streamed") + 1])
        self.assertEqual("30", options[options.index("-reconnect_delay_max") + 1])
        self.assertNotIn("-reconnect_at_eof", options)

    def test_non_http_input_options_skip_reconnect_flags(self):
        options = ffmpeg_live_input_options("file:C:/temp/live.flv")

        self.assertIn("-rw_timeout", options)
        self.assertNotIn("-reconnect", options)

    def test_uses_record_url_when_no_flv_exists(self):
        stream = SimpleNamespace(
            flv_url="",
            m3u8_url="http://cdn.example/from-attr.m3u8",
            record_url="http://cdn.example/from-record.m3u8",
        )

        url, kind = recording_input_url(stream)

        self.assertEqual("http://cdn.example/from-record.m3u8", url)
        self.assertEqual("hls", kind)
        self.assertEqual("mkv", recording_extension(url, "mkv"))

    def test_falls_back_to_m3u8_attr(self):
        stream = SimpleNamespace(
            flv_url="",
            m3u8_url="http://cdn.example/from-attr.m3u8",
            record_url="",
        )

        url, kind = recording_input_url(stream)

        self.assertEqual("http://cdn.example/from-attr.m3u8", url)
        self.assertEqual("hls", kind)

    def test_reports_missing_recording_url(self):
        stream = SimpleNamespace(flv_url="", m3u8_url="", record_url="")

        self.assertEqual(("", ""), recording_input_url(stream))
        self.assertFalse(has_recording_url(stream))


if __name__ == "__main__":
    unittest.main()
