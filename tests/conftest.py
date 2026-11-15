"""Configuration globale des tests : interdit tout accès réseau réel."""

import httpx
import pytest


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garantit qu'aucun test ne contacte Internet (autorise uniquement MockTransport)."""
    original_send = httpx.AsyncClient.send

    async def _guarded_send(self: httpx.AsyncClient, request: httpx.Request, *args, **kwargs):
        if not isinstance(self._transport, httpx.MockTransport):
            raise RuntimeError("Accès réseau interdit pendant les tests !")
        return await original_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", _guarded_send)
