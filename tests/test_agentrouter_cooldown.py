"""Exercise the actual main-loop pacing block without importing checkin.py."""

import ast
import asyncio
import math
import os
from pathlib import Path
from types import SimpleNamespace
import unittest


TARGET = Path(
    os.environ.get(
        'AGENTROUTER_TEST_TARGET',
        str(Path(__file__).resolve().parents[1] / 'checkin.py'),
    )
)


def load_cooldown_fixture(sleep, cooldown_env=''):
    tree = ast.parse(TARGET.read_text(encoding='utf-8'), filename=str(TARGET))
    main = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == 'main'
    )
    blocks = [
        statement
        for node in main.body if isinstance(node, ast.For)
        for statement in node.body if isinstance(statement, ast.If)
        if any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == 'asyncio'
            and call.func.attr == 'sleep'
            for call in ast.walk(statement)
        )
    ]
    if len(blocks) != 1:
        raise AssertionError('Expected exactly one main-loop cooldown block')

    constant = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == 'DEFAULT_BROWSER_ACCOUNT_COOLDOWN_SECONDS'
            for target in node.targets
        )
    )
    helper = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == 'get_browser_account_cooldown_seconds'
    )
    wrapper = ast.parse(
        'async def cooldown_fixture(accounts, i, app_config, total_count, account):\n'
        '    pass\n'
    ).body[0]
    wrapper.body = blocks
    module = ast.fix_missing_locations(
        ast.Module(body=[constant, helper, wrapper], type_ignores=[])
    )
    namespace = {
        'asyncio': SimpleNamespace(sleep=sleep),
        'math': math,
        'os': SimpleNamespace(
            getenv=lambda key, default='': {
                'BROWSER_ACCOUNT_COOLDOWN_SECONDS': cooldown_env,
            }.get(key, default)
        ),
    }
    exec(compile(module, str(TARGET), 'exec'), namespace)
    return namespace['cooldown_fixture']


class AgentRouterCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def observed_delays(self, providers, browser_flags, index=0, cooldown_env=''):
        delays = []

        async def record_sleep(seconds):
            delays.append(seconds)

        accounts = [SimpleNamespace(provider=provider) for provider in providers]
        configs = {
            provider: SimpleNamespace(browser_check_in=flag)
            for provider, flag in browser_flags.items()
        }
        app_config = SimpleNamespace(get_provider=configs.get)
        fixture = load_cooldown_fixture(record_sleep, cooldown_env)
        await fixture(accounts, index, app_config, len(accounts), accounts[index])
        return delays

    async def test_automatic_agentrouter_sleeps_default_60_seconds(self):
        self.assertEqual(
            await self.observed_delays(
                ['agentrouter', 'agentrouter'], {'agentrouter': False}
            ),
            [60.0],
        )

    async def test_existing_browser_cooldown_still_works(self):
        self.assertEqual(
            await self.observed_delays(
                ['browser_fixture', 'browser_fixture'], {'browser_fixture': True}
            ),
            [60.0],
        )
        self.assertEqual(
            await self.observed_delays(
                ['browser_fixture', 'browser_fixture'],
                {'browser_fixture': True},
                cooldown_env='12.5',
            ),
            [12.5],
        )

    async def test_other_automatic_provider_has_no_delay(self):
        self.assertEqual(
            await self.observed_delays(
                ['automatic_fixture', 'automatic_fixture'], {'automatic_fixture': False}
            ),
            [],
        )

    async def test_mixed_providers_have_no_delay(self):
        self.assertEqual(
            await self.observed_delays(
                ['agentrouter', 'automatic_fixture'],
                {'agentrouter': False, 'automatic_fixture': False},
            ),
            [],
        )

    async def test_last_account_has_no_delay(self):
        self.assertEqual(
            await self.observed_delays(
                ['agentrouter', 'agentrouter'], {'agentrouter': False}, index=1
            ),
            [],
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)
