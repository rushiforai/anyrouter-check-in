from types import SimpleNamespace

import pytest

import seekai_checkin
from seekai_checkin import (
	SeekAIAccount,
	bearer_headers,
	checkin_response_result,
	checkin_status,
	extract_refresh_result,
	parse_accounts,
	parse_cookies,
)


def test_parse_cookie_header_and_mapping():
	assert parse_cookies('session=abc; cf_clearance=xyz') == {'session': 'abc', 'cf_clearance': 'xyz'}
	assert parse_cookies({'session': 'abc'}) == {'session': 'abc'}
	assert parse_cookies([{'name': 'session', 'value': 'abc'}]) == {'session': 'abc'}


def test_parse_accounts_requires_auth_material():
	accounts = parse_accounts('[{"name":"one","cookies":{"session":"abc"}}]')
	assert accounts == [SeekAIAccount('one', {'session': 'abc'})]
	with pytest.raises(ValueError, match='cookies or access_token'):
		parse_accounts('[{"name":"one"}]')


def test_parse_accounts_accepts_single_object_and_api_user_alias():
	account = parse_accounts('{"name":"one","cookies":"session=abc","api_user":29704}')[0]
	assert account.user_id == '29704'
	assert account.cookies == {'session': 'abc'}


def test_bearer_headers_include_optional_session_header():
	account = SeekAIAccount('one', {}, 'TOKEN', 'SID')
	headers = bearer_headers(account)
	assert headers['Authorization'] == 'Bearer TOKEN'
	assert headers['X-Auth-Session'] == 'SID'
	assert 'new-api-user' not in headers


def test_refresh_result_validates_user_id_without_using_it_as_a_header():
	account = SeekAIAccount('one', {'session': 'abc'}, user_id='29704')
	token, user_id = extract_refresh_result({'access_token': 'NEW', 'user': {'id': 29704}}, account)
	assert token == 'NEW'
	assert user_id == '29704'
	with pytest.raises(ValueError, match='refresh user id mismatch'):
		extract_refresh_result({'access_token': 'NEW', 'user': {'id': 999}}, account)


def test_checkin_status_reads_nested_stats():
	checked, stats = checkin_status({'success': True, 'data': {'stats': {'checked_in_today': True}}})
	assert checked is True
	assert stats['checked_in_today'] is True


def test_checkin_response_handles_success_already_and_turnstile():
	result = checkin_response_result({'success': True, 'data': {'quota_awarded': 123}}, status=200)
	assert result.success is True
	assert result.quota_awarded == 123
	assert checkin_response_result({'message': 'Already checked in'}, status=200).already_checked is True
	assert checkin_response_result({'success': False, 'message': 'Turnstile required'}, status=200).success is False


@pytest.mark.asyncio
async def test_refresh_access_token_uses_response_token(monkeypatch):
	account = SeekAIAccount('one', {'session': 'abc'}, None, 'SID')
	page = SimpleNamespace(evaluate=lambda *args: None)

	async def fake_api(*args, **kwargs):
		assert kwargs['method'] == 'POST'
		assert kwargs['headers']['X-Auth-Session'] == 'SID'
		return {'status': 200, 'body': {'success': True, 'data': {'access_token': 'NEW'}}}

	monkeypatch.setattr(seekai_checkin, '_page_api', fake_api)
	assert await seekai_checkin.refresh_access_token(page, account) == 'NEW'


async def _noop(*_args, **_kwargs):
	return None


def _locator(*, count=1, enabled=True):
	async def _count():
		return count

	async def _is_enabled():
		return enabled

	return SimpleNamespace(count=_count, is_enabled=_is_enabled)


@pytest.mark.asyncio
async def test_wait_for_checkin_control_polls_until_the_button_renders(monkeypatch):
	states = ['missing', 'missing', 'ready']
	locator = _locator()

	async def fake_locate(_page):
		return states.pop(0), locator

	monkeypatch.setattr(seekai_checkin, '_locate_checkin_control', fake_locate)
	state, control = await seekai_checkin._wait_for_checkin_control(SimpleNamespace(wait_for_timeout=_noop), 5_000)
	assert state == seekai_checkin.CONTROL_READY
	assert control is locator
	assert states == []


@pytest.mark.asyncio
async def test_wait_for_checkin_control_gives_up_at_the_deadline(monkeypatch):
	calls = []

	async def fake_locate(_page):
		calls.append(1)
		return seekai_checkin.CONTROL_MISSING, None

	monkeypatch.setattr(seekai_checkin, '_locate_checkin_control', fake_locate)
	state, control = await seekai_checkin._wait_for_checkin_control(SimpleNamespace(wait_for_timeout=_noop), 0)
	assert (state, control) == (seekai_checkin.CONTROL_MISSING, None)
	assert len(calls) == 1


@pytest.mark.asyncio
async def test_click_and_complete_reports_a_signed_out_redirect(monkeypatch):
	async def fake_locate(_page):
		return seekai_checkin.CONTROL_MISSING, None

	monkeypatch.setattr(seekai_checkin, '_locate_checkin_control', fake_locate)
	page = SimpleNamespace(
		wait_for_timeout=_noop,
		url='https://seekai.cc/sign-in?redirect=%2Fprofile',
		inner_text=lambda *_args: 'Sign in',
	)
	result = await seekai_checkin.click_and_complete_checkin(page, SeekAIAccount('one', {}), 0)
	assert result.success is False
	assert 'Check in now button not found' in result.message
	assert 'no longer signed in' in result.message


@pytest.mark.asyncio
async def test_click_and_complete_treats_a_disabled_button_as_checked_in(monkeypatch):
	locator = _locator(enabled=False)

	async def fake_locate(_page):
		return seekai_checkin.CONTROL_READY, locator

	monkeypatch.setattr(seekai_checkin, '_locate_checkin_control', fake_locate)
	page = SimpleNamespace(wait_for_timeout=_noop, url='https://seekai.cc/profile', inner_text=_noop)
	result = await seekai_checkin.click_and_complete_checkin(page, SeekAIAccount('one', {}), 0)
	assert result.success is True
	assert result.already_checked is True


@pytest.mark.asyncio
async def test_click_and_complete_reports_the_rendered_page_when_the_card_is_absent(monkeypatch):
	async def fake_locate(_page):
		return seekai_checkin.CONTROL_MISSING, None

	async def fake_inner_text(_selector):
		return 'Daily Check-in\n  Check in daily to receive random quota rewards'

	monkeypatch.setattr(seekai_checkin, '_locate_checkin_control', fake_locate)
	page = SimpleNamespace(wait_for_timeout=_noop, url='https://seekai.cc/profile', inner_text=fake_inner_text)
	result = await seekai_checkin.click_and_complete_checkin(page, SeekAIAccount('one', {}), 0)
	assert result.success is False
	assert 'https://seekai.cc/profile' in result.message
	assert 'Daily Check-in' in result.message
