import threading
import unittest
from unittest.mock import patch

import douyin_media_downloader as media


class MediaBrowserLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.launcher = patch.object(media, 'ensure_media_fetch_browser', return_value={'cdp_url': media.FETCH_BROWSER_CDP, 'reused': False})
        self.launch = self.launcher.start()
        self.addCleanup(self.launcher.stop)
        self.available_patch = patch.object(media, 'cdp_is_available', return_value=True)
        self.available = self.available_patch.start()
        self.addCleanup(self.available_patch.stop)
        self.close_patch = patch.object(media, 'close_cdp_browser')
        self.close = self.close_patch.start()
        self.addCleanup(self.close_patch.stop)

    def test_posts_and_stories_release_on_success(self):
        for public, inner, result in [
            (media.fetch_posts_via_browser, '_fetch_posts_from_browser', [{'id': 'video'}]),
            (media.fetch_stories_via_browser, '_fetch_stories_from_browser', ([{'id': 'story'}], 'browser')),
        ]:
            with self.subTest(inner=inner), patch.object(media, inner, return_value=result):
                self.close.reset_mock()
                self.assertEqual(public({}, 'test-user'), result)
                self.close.assert_called_once_with(media.FETCH_BROWSER_CDP)

    def test_reused_dedicated_browser_is_released(self):
        self.launch.return_value['reused'] = True
        with media.media_fetch_browser():
            self.close.assert_not_called()
        self.close.assert_called_once_with(media.FETCH_BROWSER_CDP)

    def test_failure_and_cancellation_release_browser(self):
        for failure in [RuntimeError('fetch failed'), InterruptedError('stopping')]:
            with self.subTest(failure=failure), patch.object(media, '_fetch_posts_from_browser', side_effect=failure):
                self.close.reset_mock()
                with self.assertRaises(type(failure)):
                    media.fetch_posts_via_browser({}, 'test-user')
                self.close.assert_called_once_with(media.FETCH_BROWSER_CDP)

    def test_explicit_browser_is_borrowed(self):
        with media.media_fetch_browser('http://127.0.0.1:9999') as endpoint:
            self.assertEqual(endpoint, 'http://127.0.0.1:9999')
        self.launch.assert_not_called()
        self.close.assert_not_called()

    def test_unavailable_explicit_browser_uses_managed_fallback(self):
        self.available.side_effect = [False, True]
        with media.media_fetch_browser('http://127.0.0.1:9999') as endpoint:
            self.assertEqual(endpoint, media.FETCH_BROWSER_CDP)
        self.close.assert_called_once_with(media.FETCH_BROWSER_CDP)

    def test_failed_launch_does_not_close_unknown_browser(self):
        self.launch.side_effect = RuntimeError('launch failed')
        with self.assertRaises(RuntimeError), media.media_fetch_browser():
            self.fail('must not enter')
        self.close.assert_not_called()

    def test_already_closed_browser_does_not_trigger_cleanup(self):
        self.available.return_value = False
        with media.media_fetch_browser():
            pass
        self.close.assert_not_called()

    def test_cleanup_error_preserves_result(self):
        self.close.side_effect = RuntimeError('close failed')
        with patch.object(media, '_fetch_stories_from_browser', return_value=([], 'done')), self.assertLogs(level='WARNING'):
            self.assertEqual(media.fetch_stories_via_browser({}, 'test-user'), ([], 'done'))

    def test_second_check_waits_until_first_cleanup_finishes(self):
        cleanup_started = threading.Event()
        release_cleanup = threading.Event()
        second_started = threading.Event()
        errors = []

        def close(_):
            cleanup_started.set()
            if not release_cleanup.wait(3):
                errors.append('cleanup timed out')

        def first():
            with media.media_fetch_browser():
                pass

        def second():
            with media.media_fetch_browser():
                second_started.set()

        self.close.side_effect = close
        a = threading.Thread(target=first)
        b = threading.Thread(target=second)
        a.start()
        try:
            self.assertTrue(cleanup_started.wait(3))
            b.start()
            self.assertFalse(second_started.wait(0.1))
        finally:
            release_cleanup.set()
            a.join(3)
            if b.ident:
                b.join(3)
        self.assertFalse(a.is_alive())
        self.assertFalse(b.is_alive())
        self.assertTrue(second_started.is_set())
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
