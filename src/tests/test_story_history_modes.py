"""Archive and refresh parameters select different server views (#19)."""
import json
import pytest
from .test_story_history_retrieval import AUTHOR, UID, fetch, mobile_requests, story
import douyin_media_downloader as media


def head():
    return {'status_code': 0, 'active_data': {'data': [story('active')], 'has_more': False},
            'month_list': [], 'has_more': False, 'next_offset': 0}


def archive(item_id):
    return {'aweme_id': item_id, 'is_24_story': 1, 'story_ttl': 0,
            'create_time': 1700000000, 'author': {'sec_uid': AUTHOR, 'uid': UID}}


def refresh(extra):
    return all(extra.get(k) == v for k, v in [('source', '9'), ('pull_type', '1'), ('level', '1')])


@pytest.mark.parametrize('full_fetch', [False, True])
def test_original_archive_query_recovers_old_stories(full_fetch):
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return head()
        assert 'to_uid' in extra, 'valid archive response should not require viewer trays'
        return {'status_code': 0, 'data': [story('active')] if refresh(extra) else
                [archive('old-a'), archive('old-b'), archive('old-a'), archive('foreign') | {'author': {'sec_uid': 'other'}}],
                'has_more': False}
    with mobile_requests(response) as calls:
        result = (media.fetch_stories(object(), {'cookies': 'sessionid=test'}, AUTHOR, user_id=UID, scan_state={})
                  if full_fetch else fetch(scan_state={}))
    assert {x['aweme_id'] for x in result[0]} == {'active', 'old-a', 'old-b'}
    assert result.complete
    assert any(path == media.STORY_FEED_PATH and not refresh(extra) for path, extra, _ in calls)


def test_empty_unfinished_legacy_shape_falls_back_to_refresh():
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return head()
        assert 'to_uid' in extra
        if not refresh(extra):
            return {'status_code': 0, 'data': [], 'has_more': True, 'min_timestamp': 0, 'max_timestamp': 0}
        return {'status_code': 0, 'data': [story('refresh-only')], 'has_more': False, 'next_offset': 0}
    with mobile_requests(response) as calls:
        result = fetch(scan_state={})
    assert result.complete
    assert {x['aweme_id'] for x in result[0]} == {'active', 'refresh-only'}
    history = [extra for path, extra, _ in calls if path == media.STORY_FEED_PATH]
    assert [refresh(extra) for extra in history] == [False, True]


@pytest.mark.parametrize('legacy_state', [False, True])
def test_refresh_cursor_cannot_be_reused_as_archive_cursor(legacy_state):
    state = {}
    scan = media._story_scan_state(state, AUTHOR, 'sessionid=test')
    key = media.STORY_FEED_PATH if legacy_state else media.STORY_FEED_PATH + '#refresh'
    scan['author_offsets'][key] = '300'
    if not legacy_state:
        scan['history_mode_version'] = 1
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return head()
        assert not refresh(extra)
        if extra['offset'] == '0':
            return {'status_code': 0, 'data': [], 'has_more': True, 'next_offset': 20}
        assert extra['offset'] == '20', 'refresh cursor leaked into archive mode'
        return {'status_code': 0, 'data': [archive('old')], 'has_more': False}
    with mobile_requests(response):
        result = fetch(scan_state=state)
    assert result.complete and {x['aweme_id'] for x in result[0]} == {'active', 'old'}
    assert not state['story_scan']['author_offsets']


def test_refresh_pagination_resumes_within_shared_budget_and_after_json_reload():
    state = {}
    seen = set()
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return head()
        if not refresh(extra):
            return {'status_code': 0, 'data': [], 'has_more': True}
        offset = int(extra['offset'])
        return {'status_code': 0, 'data': [story(f'refresh-{offset}')],
                'has_more': offset < 300, 'next_offset': offset + 20}
    with mobile_requests(response) as calls:
        for poll in range(2):
            start = len(calls)
            result = fetch(scan_state=state)
            history = [e for p, e, _ in calls[start:] if p == media.STORY_FEED_PATH and 'to_uid' in e]
            assert len(history) <= 15
            assert result.complete == (poll == 1)
            seen.update(x['aweme_id'] for x in result[0])
            if poll == 0:
                assert state['story_scan']['author_offsets'][media.STORY_FEED_PATH + '#refresh'] == '280'
                assert media.STORY_FEED_PATH not in state['story_scan']['author_offsets']
            state = json.loads(json.dumps(state))
    assert seen == {'active'} | {f'refresh-{n}' for n in range(0, 301, 20)}
    assert state['story_scan']['author_offsets'] == {}


def test_partial_nonempty_archive_cannot_be_replaced_by_complete_refresh():
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return head()
        if 'to_uid' in extra:
            assert not refresh(extra), 'a nonempty archive response must retain its incomplete status'
            return {'status_code': 0, 'data': [archive('old')], 'has_more': True}
        return {'status_code': 0, 'data': [], 'has_more': False}
    with mobile_requests(response):
        result = fetch(scan_state={})
    assert not result.complete and result.scan_reason == 'pagination'
    assert {x['aweme_id'] for x in result[0]} == {'active', 'old'}


@pytest.mark.parametrize('error', [media.LoginRequiredError, InterruptedError])
def test_archive_login_and_cancellation_propagate(error):
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return head()
        raise error('fixture')
    with mobile_requests(response), pytest.raises(error):
        fetch(scan_state={})


def test_packaged_story_history_self_test():
    from release_selftest import story_history_self_test
    assert story_history_self_test() == {'archive': 3, 'refresh': 1}


def test_incomplete_archive_evidence_cannot_disappear_after_restart():
    state = {}
    first = True
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return head()
        if 'to_uid' not in extra:
            return {'status_code': 0, 'data': [], 'has_more': False}
        assert not refresh(extra), 'unfinished real archive must not become a completed refresh'
        return {'status_code': 0, 'data': [archive('old')] if first else [], 'has_more': True}
    with mobile_requests(response):
        assert not fetch(scan_state=state).complete
        state = json.loads(json.dumps(state))
        first = False
        assert not fetch(scan_state=state).complete
    assert state['story_scan']['archive_unresolved']


def test_saved_archive_cursor_survives_empty_unfinished_head():
    state = {}
    scan = media._story_scan_state(state, AUTHOR, 'sessionid=test')
    scan['history_mode_version'] = 1
    scan['author_offsets'][media.STORY_FEED_PATH] = '300'
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return head()
        assert not refresh(extra)
        if extra['offset'] == '0':
            return {'status_code': 0, 'data': [], 'has_more': True}
        assert extra['offset'] == '300'
        return {'status_code': 0, 'data': [archive('old')], 'has_more': False}
    with mobile_requests(response):
        result = fetch(scan_state=state)
    assert result.complete
    assert {x['aweme_id'] for x in result[0]} == {'active', 'old'}


def test_archive_cursor_cycle_stays_partial_and_retains_evidence():
    state = {}
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return head()
        if 'to_uid' not in extra:
            return {'status_code': 0, 'data': [], 'has_more': False}
        assert not refresh(extra)
        return {'status_code': 0, 'data': [archive('old')], 'has_more': True, 'next_offset': 20}
    with mobile_requests(response):
        result = fetch(scan_state=state)
    assert not result.complete
    assert state['story_scan']['archive_unresolved']
    assert state['story_scan']['author_offsets'][media.STORY_FEED_PATH] == '20'
