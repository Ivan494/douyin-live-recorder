import queue
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import douyin_media_downloader as media
import douyin_recorder_app as app


class AuthAlertsTest(unittest.TestCase):
    def test_standard_codec_preferred_over_shorter_bytevc2_url(self):
        item={'video':{'bit_rate':[{'is_bytevc1':2,'play_addr':{'url_list':['https://cdn.test/x']}}], 'play_addr_h264':{'url_list':['https://cdn.test/compatible-h264-video']}, 'play_addr_265':{'url_list':['https://cdn.test/hevc-video']}}}
        urls=media.collect_video_urls(item)
        self.assertEqual(urls[0],'https://cdn.test/compatible-h264-video')
        self.assertEqual(urls[1],'https://cdn.test/hevc-video')

    def test_explicit_login_responses_are_classified(self):
        for code in (8, 12, 2483, '2483'):
            with self.subTest(code=code), self.assertRaises(media.LoginRequiredError):
                media._raise_mobile_login_required({'status_code': code})
        media._raise_mobile_login_required({'status_code': 0, 'aweme_list': []})
        media._raise_mobile_login_required({'status_code': 500})

    def test_mobile_posts_propagate_login_failure(self):
        response=Mock()
        response.json.return_value={'status_code': 8}
        with patch.object(media, '_check_mobile_signer', return_value=True), patch.object(media, '_persistent_mobile_device', return_value=('1','2')), patch.object(media, '_mobile_device_profile', return_value={}), patch.object(media, '_mobile_signed_request', return_value=response), patch.object(media, '_mobile_signed_get', return_value=response):
            with self.assertRaises(media.LoginRequiredError):
                media.fetch_posts_via_mobile_api(Mock(), 'uid', cookie_header='fake')
            with self.assertRaises(media.LoginRequiredError):
                media.fetch_stories_via_mobile_post_api(Mock(), 'uid', cookie_header='fake')
            with self.assertRaises(media.LoginRequiredError):
                media.fetch_stories_via_mobile_story_feed(Mock(), 'uid', user_id='123', cookie_header='fake')
            with self.assertRaises(media.LoginRequiredError):
                media.fetch_stories_via_mobile_life_feed(Mock(), 'uid', user_id='123', cookie_header='fake')

    def test_story_only_alert_deduplicates_and_rearms_after_recovery(self):
        with patch.object(app.MediaDownloadEngine, '_load_circuit_breaker_state'):
            engine=app.MediaDownloadEngine(Mock(), queue.Queue(), notify_callback=Mock())
        p={'id':'a','name':'Test'}
        bad={'videos':{'status':'disabled'},'stories':{'status':'login_required'}}
        with patch.object(app.time, 'monotonic', return_value=100):
            engine._notify_media_attention(p,bad)
            engine._notify_media_attention(p,bad)
        self.assertEqual(engine.notify_callback.call_count,1)
        with patch.object(app.time, 'monotonic', return_value=2000):
            engine._notify_media_attention(p,bad)
        self.assertEqual(engine.notify_callback.call_count,2)
        engine._notify_media_attention(p,{'stories':{'status':'ok'}})
        engine._notify_media_attention(p,bad)
        self.assertEqual(engine.notify_callback.call_count,3)

    def test_fallback_success_does_not_hide_expired_app_login(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(media, '_mobile_cookie_header', return_value='fake'), patch.object(media, 'fetch_posts_via_mobile_api', side_effect=media.LoginRequiredError('expired')), patch.object(media, 'fetch_posts', return_value=[{'aweme_id':'1'}]), patch.object(media, 'fetch_posts_via_browser') as browser, patch.object(media, 'download_aweme_items', return_value=media.MediaResult(status='ok')):
            summary=media.download_profile({'id':'a','name':'Test','output_dir':tmp,'original_profile_url':'https://www.douyin.com/user/test'}, videos=True, stories=False)
        self.assertEqual(summary['videos']['status'],'ok')
        self.assertEqual(summary['auth_warning'],'login_required')
        browser.assert_not_called()

    def test_login_summary_backs_off_and_updates_status(self):
        p={'id':'a','name':'Test','auto_download_stories':True}
        store=SimpleNamespace(settings={}, get_profile=lambda _:p)
        with patch.object(app.MediaDownloadEngine,'_load_circuit_breaker_state'):
            engine=app.MediaDownloadEngine(store,queue.Queue(),notify_callback=Mock())
        with patch.object(app,'download_profile',return_value={'videos':{'status':'disabled'},'stories':{'status':'login_required'}}),patch.object(engine,'_save_circuit_breaker_state'):
            engine._check_profile(p)
        self.assertEqual(engine.consecutive_failures['a'],1)
        engine.notify_callback.assert_called_once()
        self.assertTrue(engine.event_queue.get()['state']['media_status'])
