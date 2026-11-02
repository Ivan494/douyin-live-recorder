"""Independent response-view audit: preserve both released views, not one mode."""
import json
import httpx
import pytest
import douyin_media_downloader as media
from .test_story_history_retrieval import AUTHOR, UID, story, mobile_requests, fetch


def profile(items=()):
    return {'status_code': 0, 'active_data': {'data': [story(x) for x in items], 'has_more': False},
            'has_more': False, 'next_offset': 0}


@pytest.mark.parametrize('profile_ids', [[], ['active']])
@pytest.mark.parametrize('archive_shape', ['empty', 'archives', 'unavailable', 'error'])
@pytest.mark.parametrize('refresh_ids', [[], ['refresh-only'], ['active', 'refresh-only']])
def test_union_of_independent_archive_and_refresh_views(profile_ids, archive_shape, refresh_ids):
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return profile(profile_ids)
        if 'to_uid' not in extra:
            return {'status_code': 0, 'data': [], 'has_more': False}
        if extra.get('source') == '9':
            return {'status_code': 0, 'data': [story(x) for x in refresh_ids], 'has_more': False}
        if archive_shape == 'error':
            raise httpx.ReadTimeout('archive transport failure')
        if archive_shape == 'unavailable':
            return {'status_code': 0, 'data': [], 'has_more': True}
        return {'status_code': 0, 'data': [story(x) for x in
                (['old-a', 'old-b'] if archive_shape == 'archives' else [])], 'has_more': False}
    with mobile_requests(response) as calls:
        result = fetch(scan_state={})
    expected = set(profile_ids) | set(refresh_ids)
    if archive_shape == 'archives':
        expected |= {'old-a', 'old-b'}
    ids = [x['aweme_id'] for x in result[0] or []]
    assert set(ids) == expected
    assert len(ids) == len(set(ids))
    assert result.complete == (archive_shape != 'error')
    modes = {e.get('source', 'archive') for p, e, _ in calls if p == media.STORY_FEED_PATH and 'to_uid' in e}
    assert modes == {'archive', '9'}


@pytest.mark.parametrize('reload_state', [False, True])
def test_both_paginated_views_advance_with_distinct_cursor_namespaces(reload_state):
    state = {}
    seen = set()
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return profile()
        if 'to_uid' not in extra:
            return {'status_code': 0, 'data': [], 'has_more': False}
        refresh = extra.get('source') == '9'
        base = 2000 if refresh else 1000
        offset = int(extra['offset'])
        index = 0 if offset == 0 else offset - base
        assert 0 <= index < 20, 'cursor crossed request-mode namespace'
        return {'status_code': 0, 'data': [story(f'{base}-{index}')],
                'has_more': index < 19, 'next_offset': base + index + 1}
    with mobile_requests(response) as calls:
        for poll in range(3):
            start = len(calls)
            result = fetch(scan_state=state)
            current = [e for p, e, _ in calls[start:] if p == media.STORY_FEED_PATH and 'to_uid' in e]
            assert len(current) <= 15
            assert all(any(e.get('source') == source and e['offset'] != '0' for e in current)
                       for source in (None, '9'))
            assert result.complete == (poll == 2)
            seen.update(x['aweme_id'] for x in result[0] or [])
            if reload_state:
                state = json.loads(json.dumps(state))
    assert seen == {f'{base}-{n}' for base in (1000, 2000) for n in range(20)}
    assert state['story_scan']['author_offsets'] == {}


def test_ambiguous_old_checkpoint_is_revalidated_in_each_view():
    state = {}
    scan = media._story_scan_state(state, AUTHOR, 'sessionid=test')
    scan['author_offsets'][media.STORY_FEED_PATH] = '999999'
    seen = []
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return profile()
        refresh = extra.get('source') == '9'
        offset = extra['offset']
        seen.append((refresh, offset))
        if not refresh:
            assert offset == '0'
            return {'status_code': 0, 'data': [story('archive')], 'has_more': False}
        return {'status_code': 0, 'data': [story('refresh-' + offset)],
                'has_more': offset == '0', 'next_offset': 2001}
    with mobile_requests(response):
        result = fetch(scan_state=state)
    assert result.complete
    assert seen == [(False, '0'), (True, '0'), (True, '2001')]
    assert {x['aweme_id'] for x in result[0]} == {'archive', 'refresh-0', 'refresh-2001'}


def test_two_unsupported_views_do_not_claim_exhaustion():
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return profile(['active'])
        return {'status_code': 0, 'data': [], 'has_more': '1'}
    with mobile_requests(response):
        result = fetch(scan_state={})
    assert not result.complete and result.scan_reason == 'pagination'


@pytest.mark.parametrize('version', [None, 1, 2])
def test_upgrade_revalidates_only_ambiguous_mode_cursors(version):
    state = {}
    scan = media._story_scan_state(state, AUTHOR, 'sessionid=test')
    scan['author_offsets'] = {media.STORY_FEED_PATH: '1234', media.STORY_FEED_PATH + '#refresh': '2345'}
    scan['post_cursor'] = '8888'
    if version is not None:
        scan['history_mode_version'] = version
    expected_archive = '1001' if version is None else '1234'
    expected_refresh = '2345' if version == 2 else '2001'
    seen = []
    def response(path, extra, _host):
        if path == media.STORY_PROFILE_LIST_PATH:
            return profile()
        refresh = extra.get('source') == '9'
        offset = extra['offset']
        seen.append((refresh, offset))
        if offset != '0':
            assert offset == (expected_refresh if refresh else expected_archive)
        return {'status_code': 0, 'data': [story(str(refresh) + offset)],
                'has_more': offset == '0', 'next_offset': 2001 if refresh else 1001}
    with mobile_requests(response):
        result = fetch(scan_state=state)
    assert result.complete
    assert (False, expected_archive) in seen and (True, expected_refresh) in seen
    assert state['story_scan']['history_mode_version'] == 2
    assert state['story_scan']['post_cursor'] == '8888'
    assert state['story_scan']['author_offsets'] == {}
