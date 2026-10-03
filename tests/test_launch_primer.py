"""Connect at launch: the held connection opens when Chrome starts, while you are in it.

prime() runs against the strict fake Chrome from test_cdp_relay_hold. The
LaunchPrimer decision runs with stub probes and a fake clock, so every branch
is exercised without a Mac, a Chrome or a prompt.
"""
import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cdp_relay
from cdp_relay import Endpoint, LaunchPrimer
import test_cdp_relay_hold as hold


class PrimeTests(hold.HeldRelayFixture):
    """prime() on a real held connection, with the fake Chrome and helpers."""

    async def test_prime_opens_and_the_first_client_needs_no_prompt(self):
        self.assertTrue(await self.relay.held.prime())
        self.assertEqual((self.chrome.connections, self.focus.calls), (1, 1))
        ws = await self.client()
        await self.call(ws, "Runtime.evaluate")
        self.assertEqual((self.chrome.connections, self.focus.calls), (1, 1))

    async def test_prime_when_already_open_does_nothing(self):
        await self.relay.held.prime()
        self.assertFalse(await self.relay.held.prime())
        self.assertEqual(self.chrome.connections, 1)

    async def test_prime_never_takes_the_connection_from_a_client(self):
        ws = await self.client()
        await self.call(ws, "Runtime.evaluate")
        self.assertFalse(await self.relay.held.prime())
        self.assertEqual(self.chrome.connections, 1)
        self.assertIs(self.relay.held.owner is not None, True)

    async def test_prime_after_a_chrome_restart_opens_a_new_connection(self):
        await self.relay.held.prime()
        self.write_endpoint("/devtools/browser/restarted")
        self.assertTrue(await self.relay.held.prime())
        self.assertEqual(self.chrome.connections, 2)


# Stubs for the decision tests below.
CHROME_PID, OTHER_PID = 101, 202
FIRST, SECOND = Endpoint(9222, "/devtools/browser/first"), Endpoint(9222, "/devtools/browser/second")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class LaunchPrimerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.endpoint = None
        self.front = CHROME_PID
        self.idle = 30.0
        self.clock = Clock()
        self.held = Mock(open=False, endpoint=None)
        self.held.busy.return_value = False
        self.held.prime = AsyncMock(return_value=True)

    def primer(self):
        async def owner(endpoint):
            return CHROME_PID
        async def front():
            return self.front
        return LaunchPrimer(self.held, lambda: self.endpoint, owner, front, lambda: self.idle, clock=self.clock)

    async def test_a_chrome_already_running_when_the_relay_starts_is_never_prompted(self):
        self.endpoint = FIRST
        primer = self.primer()
        for _ in range(3):
            self.assertEqual(await primer.tick(), "idle")
        self.held.prime.assert_not_called()

    async def test_a_new_chrome_in_front_and_idle_is_primed_once(self):
        primer = self.primer()
        self.endpoint = FIRST
        self.assertEqual(await primer.tick(), "primed")
        self.assertEqual(await primer.tick(), "idle")
        self.held.prime.assert_awaited_once()

    async def test_it_waits_while_another_app_is_in_front(self):
        primer = self.primer()
        self.endpoint, self.front = FIRST, OTHER_PID
        self.assertEqual(await primer.tick(), "not-in-front")
        self.front = None                        # Chrome active but no window on top yet
        self.assertEqual(await primer.tick(), "not-in-front")
        self.front = CHROME_PID
        self.assertEqual(await primer.tick(), "primed")

    async def test_it_waits_while_you_are_typing(self):
        primer = self.primer()
        self.endpoint, self.idle = FIRST, cdp_relay.PRIME_IDLE_S - 0.1
        self.assertEqual(await primer.tick(), "input")
        self.held.prime.assert_not_called()
        self.idle = cdp_relay.PRIME_IDLE_S
        self.assertEqual(await primer.tick(), "primed")

    async def test_it_gives_up_after_the_window_and_leaves_it_to_the_first_client(self):
        primer = self.primer()
        self.endpoint, self.front = FIRST, OTHER_PID
        await primer.tick()
        self.clock.now += cdp_relay.PRIME_WINDOW_S + 1
        self.front = CHROME_PID
        self.assertEqual(await primer.tick(), "gave-up")
        self.assertEqual(await primer.tick(), "idle")
        self.held.prime.assert_not_called()

    async def test_one_attempt_per_launch_even_if_it_fails(self):
        self.held.prime = AsyncMock(side_effect=RuntimeError("denied"))
        primer = self.primer()
        self.endpoint = FIRST
        self.assertEqual(await primer.tick(), "failed")
        for _ in range(3):
            self.assertEqual(await primer.tick(), "idle")
        self.held.prime.assert_awaited_once()

    async def test_a_client_that_got_there_first_ends_the_wait(self):
        primer = self.primer()
        self.endpoint, self.front = FIRST, OTHER_PID
        await primer.tick()
        self.held.open, self.held.endpoint = True, FIRST
        self.assertEqual(await primer.tick(), "already-open")
        self.assertEqual(await primer.tick(), "idle")

    async def test_busy_waits_rather_than_competing(self):
        primer = self.primer()
        self.endpoint = FIRST
        self.held.busy.return_value = True
        self.assertEqual(await primer.tick(), "busy")
        self.held.busy.return_value = False
        self.assertEqual(await primer.tick(), "primed")

    async def test_every_launch_gets_its_own_chance(self):
        primer = self.primer()
        self.endpoint = FIRST
        self.assertEqual(await primer.tick(), "primed")
        self.endpoint = SECOND
        self.assertEqual(await primer.tick(), "primed")
        self.assertEqual(self.held.prime.await_count, 2)

    async def test_chrome_quitting_while_waiting_stops_the_wait(self):
        primer = self.primer()
        self.endpoint, self.front = FIRST, OTHER_PID
        await primer.tick()
        self.endpoint = None
        self.assertEqual(await primer.tick(), "chrome-gone")
        self.assertEqual(await primer.tick(), "idle")


if __name__ == "__main__":
    unittest.main()
