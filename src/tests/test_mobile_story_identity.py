from unittest.mock import patch
import httpx
import pytest
import douyin_media_downloader as media


def response(payload):
    return httpx.Response(200, json=payload, request=httpx.Request('GET', 'https://example.test/profile'))


def test_mobile_identity_succeeds_without_the_blocked_web_endpoint():
    payload = {'status_code': 0, 'user': {'uid': '123456', 'sec_uid': 'target'}}
    with patch.object(media, '_check_mobile_signer', return_value=True), patch.object(
        media, '_persistent_mobile_device', return_value=('1', '2')
    ), patch.object(media, '_mobile_signed_request', return_value=response(payload)) as mobile, patch.object(
        media, 'request_json', side_effect=media.EmptyApiResponseError('web unavailable')
    ) as web:
        assert media.resolve_numeric_user_id(object(), {'cookies': 'sessionid=test'}, 'target') == '123456'
    web.assert_not_called()
    assert mobile.call_args.args[2:4] == ('/aweme/v1/user/profile/other/', {'sec_user_id': 'target'})


@pytest.mark.parametrize('user', [{'uid': 'wrong'}, {'uid': '123456', 'sec_uid': 'other'}])
def test_mobile_identity_requires_a_matching_author_then_falls_back(user):
    with patch.object(media, '_check_mobile_signer', return_value=True), patch.object(
        media, '_persistent_mobile_device', return_value=('1', '2')
    ), patch.object(media, '_mobile_signed_request', return_value=response({'status_code': 0, 'user': user})), patch.object(
        media, 'request_json', return_value={'user': {'uid': '654321', 'sec_uid': 'target'}}
    ):
        assert media.resolve_numeric_user_id(object(), {'cookies': 'sessionid=test'}, 'target') == '654321'


@pytest.mark.parametrize('error', [InterruptedError('cancelled'), media.LoginRequiredError('expired')])
def test_mobile_identity_cancellation_and_login_rejection_propagate(error):
    with patch.object(media, '_check_mobile_signer', return_value=True), patch.object(
        media, '_persistent_mobile_device', return_value=('1', '2')
    ), patch.object(media, '_mobile_signed_request', side_effect=error), pytest.raises(type(error)):
        media.resolve_numeric_user_id(object(), {'cookies': 'sessionid=test'}, 'target')
