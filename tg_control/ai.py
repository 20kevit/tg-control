"""Synthetic task contract only; no approved utility catalog or external provider."""
from .core import GatewayError, Request


class SyntheticTasks:
    def __init__(self, gateway):
        self.gateway = gateway

    def invoke(self, client, account, chat, message_ids, *, explicit=False):
        if explicit is not True or not 1 <= len(message_ids) <= 10:
            raise GatewayError('explicit_bounded_invocation_required')
        # Gateway rejects all non-synthetic adapters; caller cannot override provenance.
        outcomes = self.gateway.batch(client, [Request(account, 'read', chat, m) for m in message_ids])
        if not all(o['ok'] for o in outcomes):
            raise GatewayError('input_unavailable')
        return {'task': 'fixture_character_count',
                'counts': [len(o['result'].text) for o in outcomes]}
