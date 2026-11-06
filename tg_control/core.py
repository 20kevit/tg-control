from dataclasses import dataclass, field, replace
from typing import Protocol
import time


class GatewayError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class RateLimited(GatewayError):
    def __init__(self, seconds):
        super().__init__('rate_limited')
        self.seconds = seconds


@dataclass(frozen=True)
class Chat:
    id: str
    kind: str = 'private'


@dataclass(frozen=True)
class Message:
    id: str
    text: str = ''
    reply_to: str | None = None
    kind: str = 'text'
    caption: str = ''
    filename: str = ''
    media: bytes | None = None


@dataclass(frozen=True)
class Grant:
    client: str
    account: str
    permission: str
    chats: frozenset = frozenset()
    public_types: frozenset = frozenset()
    exclusions: frozenset = frozenset()


class Policy:
    def __init__(self, grants=()):
        self.grants = list(grants)

    def allows(self, client, account, permission, chat):
        grants = [g for g in self.grants if
                  (g.client, g.account, g.permission) == (client, account, permission)]
        if any(chat.id in g.exclusions for g in grants):
            return False
        return any(chat.id in g.chats or
                   (chat.kind in {'public_group', 'public_channel'} and
                    chat.kind in g.public_types) for g in grants)


class Adapter(Protocol):
    chats: dict[str, Chat]
    def get(self, chat: str, message: str) -> Message: ...
    def search(self, chat: str, query: str) -> list[Message]: ...
    def send(self, chat: str, message: Message) -> Message: ...
    def download(self, message: Message) -> bytes: ...


class SyntheticAdapter:
    """No network, filesystem or native session access."""
    def __init__(self, chats, messages=None):
        self.chats = dict(chats)
        self.messages = dict(messages or {})
        self.sent = []
        self.failure = None

    def get(self, chat, message):
        try:
            return self.messages[chat, message]
        except KeyError:
            raise GatewayError('message_unavailable') from None

    def search(self, chat, query):
        return [m for (c, _), m in self.messages.items() if c == chat and query in m.text]

    def download(self, message):
        if self.failure == 'download':
            raise GatewayError('download_failed')
        if message.media is None:
            raise GatewayError('media_unavailable')
        return message.media

    def send(self, chat, message):
        if self.failure == 'upload':
            raise GatewayError('upload_failed')
        self.sent.append((chat, message))
        return message


@dataclass(frozen=True)
class Request:
    account: str
    operation: str
    chat: str
    message: str = ''
    query: str = ''
    content: Message | None = None


class Gateway:
    permissions = {'read': 'read', 'search': 'read', 'send': 'write', 'save': 'read'}

    def __init__(self, accounts, policy, sleep=time.sleep, retry_budget=2):
        # Production adapters cannot be injected into this compliance-closed build.
        if any(type(a) is not SyntheticAdapter for a in accounts.values()):
            raise GatewayError('compliance_closed')
        self.accounts = dict(accounts)
        self.policy = policy
        self.audit = []
        self.sleep = sleep
        self.retry_budget = retry_budget

    def authorize(self, client, req):
        adapter = self.accounts.get(req.account)
        if adapter is None or req.operation not in self.permissions:
            raise GatewayError('invalid_request')
        chat = adapter.chats.get(req.chat)
        if chat is None or not self.policy.allows(client, req.account,
                                                  self.permissions[req.operation], chat):
            raise GatewayError('permission_denied')
        if req.operation == 'save':
            dest = adapter.chats.get('self')
            if dest is None or not self.policy.allows(client, req.account, 'write', dest):
                raise GatewayError('permission_denied')
        if req.operation == 'send' and not isinstance(req.content, Message):
            raise GatewayError('invalid_request')
        return adapter

    def _run(self, adapter, req):
        if req.operation == 'read':
            return adapter.get(req.chat, req.message)
        if req.operation == 'search':
            return adapter.search(req.chat, req.query)
        if req.operation == 'send':
            return adapter.send(req.chat, req.content)
        command = adapter.get(req.chat, req.message)
        if not command.reply_to:
            raise GatewayError('missing_reply')
        source = adapter.get(req.chat, command.reply_to)
        if source.kind not in {'text', 'photo', 'video', 'audio', 'voice', 'document', 'animation', 'sticker'}:
            raise GatewayError('unsupported_content')
        if source.kind != 'text':
            source = replace(source, media=adapter.download(source))
        return adapter.send('self', replace(source, reply_to=None))

    def _record(self, client, req, outcome):
        self.audit.append({'client': client, 'account': req.account, 'chat': req.chat,
                           'operation': req.operation, 'outcome': outcome, 'time': time.time()})

    def execute(self, client, req):
        try:
            adapter = self.authorize(client, req)
            spent = 0
            for attempt in range(3):
                try:
                    result = self._run(adapter, req)
                    break
                except RateLimited as error:
                    if error.seconds < 0 or spent + error.seconds > self.retry_budget or attempt == 2:
                        raise GatewayError('rate_limit_timeout') from None
                    spent += error.seconds
                    self.sleep(error.seconds)
            self._record(client, req, 'ok')
            return result
        except GatewayError as error:
            self._record(client, req, error.code)
            raise

    def batch(self, client, requests):
        requests = list(requests)
        if not 1 <= len(requests) <= 100:
            raise GatewayError('invalid_batch')
        try:
            for req in requests:
                self.authorize(client, req)
        except GatewayError:
            for req in requests:
                self._record(client, req, 'batch_preflight_denied')
            raise
        results = []
        for req in requests:
            try:
                results.append({'ok': True, 'result': self.execute(client, req)})
            except GatewayError as error:
                results.append({'ok': False, 'error': error.code})
        return results

    def prune_audit(self, days=90):
        if days < 1:
            raise GatewayError('invalid_retention')
        cutoff = time.time() - days * 86400
        self.audit[:] = [a for a in self.audit if a['time'] >= cutoff]


class CommandRouter:
    def __init__(self, gateway):
        self.gateway = gateway

    def invoke(self, client, command, account, chat, message):
        if command != '/save':
            raise GatewayError('unknown_command')
        return self.gateway.execute(client, Request(account, 'save', chat, message))
