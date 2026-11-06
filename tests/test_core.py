import unittest
from tg_control.core import *
from tg_control.ai import SyntheticTasks


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.adapter = SyntheticAdapter({'c': Chat('c'), 'self': Chat('self'),
                                         'public': Chat('public', 'public_group')}, {
            ('c', '1'): Message('1', text='opaque /save text'),
            ('c', '2'): Message('2', reply_to='1')})
        self.policy = Policy([Grant('u', 'a', 'read', frozenset({'c'})),
                              Grant('u', 'a', 'write', frozenset({'self'}))])
        self.gateway = Gateway({'a': self.adapter}, self.policy, sleep=lambda _: None)

    def error(self, code, request, client='u'):
        with self.assertRaises(GatewayError) as caught:
            self.gateway.execute(client, request)
        self.assertEqual(caught.exception.code, code)

    def test_read_search_and_opaque_content(self):
        self.assertEqual(self.gateway.execute('u', Request('a', 'search', 'c', query='/save'))[0].id, '1')
        self.assertEqual(self.adapter.sent, [])
        self.assertNotIn('opaque', repr(self.gateway.audit))

    def test_permissions_independent(self):
        self.error('permission_denied', Request('a', 'send', 'c', content=Message('x')))
        self.policy.grants = [Grant('u', 'a', 'admin', frozenset({'c'}))]
        self.error('permission_denied', Request('a', 'read', 'c', '1'))

    def test_exclusion_overrides(self):
        self.policy.grants.append(Grant('u', 'a', 'read', exclusions=frozenset({'c'})))
        self.error('permission_denied', Request('a', 'read', 'c', '1'))

    def test_private_not_type_granted(self):
        self.policy.grants = [Grant('u', 'a', 'read', public_types=frozenset({'private', 'public_group'}))]
        self.error('permission_denied', Request('a', 'read', 'c', '1'))
        self.assertEqual(self.gateway.execute('u', Request('a', 'search', 'public')), [])

    def test_client_account_isolation(self):
        self.error('permission_denied', Request('a', 'read', 'c', '1'), 'other')
        self.error('invalid_request', Request('b', 'read', 'c', '1'))
        other = SyntheticAdapter(self.adapter.chats, {('c', '1'): Message('1', text='other')})
        self.gateway.accounts['b'] = other
        self.error('permission_denied', Request('b', 'read', 'c', '1'))
        self.assertEqual(other.sent, [])

    def test_save_text_and_router(self):
        result = CommandRouter(self.gateway).invoke('u', '/save', 'a', 'c', '2')
        self.assertEqual(result.text, 'opaque /save text')
        self.assertEqual(self.adapter.sent[0][0], 'self')

    def test_save_missing_reply(self):
        self.error('missing_reply', Request('a', 'save', 'c', '1'))

    def test_save_media_preserves(self):
        self.adapter.messages['c', '1'] = Message('1', kind='document', caption='caption', filename='../opaque', media=b'fixture')
        result = self.gateway.execute('u', Request('a', 'save', 'c', '2'))
        self.assertEqual((result.caption, result.filename, result.media), ('caption', '../opaque', b'fixture'))

    def test_save_failures(self):
        req = Request('a', 'save', 'c', '2')
        self.adapter.messages['c', '1'] = Message('1', kind='unsupported')
        self.error('unsupported_content', req)
        self.adapter.messages['c', '1'] = Message('1', kind='photo')
        self.error('media_unavailable', req)
        self.adapter.failure = 'download'
        self.error('download_failed', req)
        self.adapter.messages['c', '1'] = Message('1', kind='photo', media=b'x')
        self.adapter.failure = 'upload'
        self.error('upload_failed', req)

    def test_save_write_denied_before_read(self):
        self.policy.grants.pop()
        self.error('permission_denied', Request('a', 'save', 'c', '2'))
        self.assertEqual(self.adapter.sent, [])

    def test_batch_preflight(self):
        with self.assertRaises(GatewayError):
            self.gateway.batch('u', [Request('a', 'send', 'self', content=Message('x')),
                                     Request('a', 'send', 'c', content=Message('x'))])
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(len(self.gateway.audit), 2)

    def test_per_item_outcomes(self):
        results = self.gateway.batch('u', [Request('a', 'read', 'c', '1'), Request('a', 'read', 'c', 'missing')])
        self.assertTrue(results[0]['ok'])
        self.assertEqual(results[1]['error'], 'message_unavailable')

    def test_rate_limit_bounded(self):
        self.adapter.send = lambda *args: (_ for _ in ()).throw(RateLimited(3))
        self.error('rate_limit_timeout', Request('a', 'save', 'c', '2'))

    def test_rate_limit_retry(self):
        original = self.adapter.send
        calls = []
        def send(*args):
            calls.append(1)
            if len(calls) == 1:
                raise RateLimited(1)
            return original(*args)
        self.adapter.send = send
        self.gateway.execute('u', Request('a', 'save', 'c', '2'))
        self.assertEqual(len(calls), 2)

    def test_compliance_closed(self):
        with self.assertRaises(GatewayError):
            Gateway({'live': object()}, self.policy)

    def test_ai_explicit_and_scoped(self):
        tasks = SyntheticTasks(self.gateway)
        with self.assertRaises(GatewayError):
            tasks.invoke('u', 'a', 'c', ['1'])
        self.assertEqual(tasks.invoke('u', 'a', 'c', ['1'], explicit=True)['counts'], [17])
        with self.assertRaises(GatewayError):
            tasks.invoke('u', 'a', 'self', ['1'], explicit=True)

    def test_retention(self):
        self.gateway.audit = [{'time': 0}]
        self.gateway.prune_audit()
        self.assertEqual(self.gateway.audit, [])

    def test_invalid_requests(self):
        for req in (None, Request('', 'read', 'c', '1'), Request('a', 'read', 'c'),
                    Request('a', 'search', 'c', query='x' * 4097)):
            with self.assertRaisesRegex(GatewayError, 'invalid_request'):
                self.gateway.execute('u', req)
        self.assertEqual(self.adapter.sent, [])

    def test_adapter_error_normalized_without_secret(self):
        def fail(*args):
            raise OSError('secret exception payload')
        self.adapter.send = fail
        self.error('adapter_failure', Request('a', 'save', 'c', '2'))
        self.assertNotIn('secret', repr(self.gateway.audit))

    def test_nonfinite_rate_limit(self):
        for wait in (float('nan'), float('inf'), -1, 'invalid'):
            self.adapter.send = lambda *args: (_ for _ in ()).throw(RateLimited(wait))
            self.error('rate_limit_timeout', Request('a', 'save', 'c', '2'))

    def test_audit_intent_failure_blocks_effect(self):
        class FailedAudit:
            def append(self, event):
                raise GatewayError('storage_unavailable')
        self.gateway.audit_sink = FailedAudit()
        self.error('storage_unavailable', Request('a', 'save', 'c', '2'))
        self.assertEqual(self.adapter.sent, [])

    def test_audit_completion_failure_marks_unknown(self):
        class FailingCompletion:
            def append(self, event):
                if event['outcome'] == 'ok':
                    raise GatewayError('storage_unavailable')
        self.gateway.audit_sink = FailingCompletion()
        self.error('audit_outcome_unknown', Request('a', 'save', 'c', '2'))
        self.assertEqual(len(self.adapter.sent), 1)
