"""Independent fault injection: durable state, ambiguous saves, recurring titles."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import monitor as m
from test_monitor import item, state, NOW, USER


class DeliveryAuditTests(unittest.TestCase):
    def setUp(self):
        self.s = m.plan(state(), [item()], NOW)
        self.identity, self.article = next(iter(self.s['queue'].items()))
        self.payload = patch.object(m, 'payload', return_value={})
        self.payload.start()
        self.addCleanup(self.payload.stop)

    def test_reservation_failure_no_post_or_phantom_local_pending(self):
        posts = []
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(m.DeliveryPersistenceError):
                m.deliver(self.s, self.identity, self.article,
                          lambda s: (_ for _ in ()).throw(TimeoutError()),
                          lambda *args: posts.append(args), USER, NOW)
        self.assertFalse(posts)
        self.assertFalse(self.s['pending'])
        self.assertIn(self.identity, self.s['queue'])

    def test_receipt_save_failure_preserves_pending_and_logs_evidence(self):
        durable = []
        posts = []
        def save(s):
            if durable:
                raise TimeoutError()
            durable.append(copy.deepcopy(s))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(m.DeliveryPersistenceError):
                m.deliver(self.s, self.identity, self.article, save,
                          lambda *args: posts.append(args) or {'id': '123456789012345678'}, USER, NOW)
        self.assertEqual(len(posts), 1)
        self.assertTrue(durable[0]['pending'])
        self.assertEqual(self.s, durable[0])
        self.assertIn('123456789012345678', out.getvalue())
        self.assertFalse(m.deliver(self.s, self.identity, self.article, save,
                                  lambda *args: posts.append(args), USER, NOW))
        self.assertEqual(len(posts), 1)

    def test_rejection_cleanup_save_failure_stops_with_durable_pending(self):
        durable = []
        def save(s):
            if durable:
                raise TimeoutError()
            durable.append(copy.deepcopy(s))
        def send(*args):
            raise HTTPError('redacted', 429, 'rejected', {}, None)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(m.DeliveryPersistenceError):
                m.deliver(self.s, self.identity, self.article, save, send, USER, NOW)
        self.assertEqual(self.s, durable[0])

    def test_same_heading_on_new_publication_day_is_new_article(self):
        for title in ('Developer Update', "Director's Take"):
            old = item(title=title, gid='old', url='https://example.com/old', date=NOW-2*m.DAY)
            new = item(title=title, gid='new', url='https://example.com/new', date=NOW-100)
            s = state()
            s['sent'] = sorted(m.scoped_keys(old))
            self.assertEqual(len(m.plan(s, [new], NOW)['queue']), 1)

    def test_legacy_permanent_headlines_not_reintroduced_by_localization(self):
        a = item(original_keys=['title:old', 'headline-slug:2026:director-s-take', 'gid:mirror'])
        self.assertNotIn('title:old', m.keys(a))
        self.assertNotIn('headline-slug:2026:director-s-take', m.keys(a))
        self.assertIn('gid:mirror', m.keys(a))

    def test_confirmed_strong_identity_survives_title_date_change(self):
        old = item(title='Developer Update', date=NOW-2*m.DAY)
        new = dict(old, title='Renamed Developer Update', date=NOW-100)
        s = state()
        s['sent'] = sorted(m.scoped_keys(old))
        self.assertFalse(m.plan(s, [new], NOW)['queue'])


class StateValidationAuditTests(unittest.TestCase):
    def valid(self):
        return dict(state(), checked={}, source_failures={})

    def test_receipt_container_rejected_during_store_load(self):
        class FakeStore(m.Store):
            def __init__(self, saved):
                self.saved = saved
            def read(self, *args):
                return self.saved, 'sha'
        for invalid in (None, {}, 'corrupt'):
            s = self.valid()
            s['receipts'] = invalid
            with self.assertRaises(ValueError):
                FakeStore(s).load(NOW)

    def test_confirmed_receipt_missing_from_sent_refuses_reset(self):
        s = self.valid()
        s['receipts'] = [{'id': '123', 'keys': ['ow:gid:confirmed']}]
        with self.assertRaises(ValueError):
            m.validate_state(s)
        s['sent'] = ['ow:gid:confirmed']
        m.validate_state(s)

    def test_pending_wrong_route_and_invalid_queue_fail_closed(self):
        s = self.valid()
        s['pending']['game-monitor_apex'] = dict(topic='ow', keys=['ow:gid:a'], queue_id='q')
        with self.assertRaises(ValueError):
            m.validate_state(s)
        s['pending'] = {}
        s['queue']['q'] = {'topic': 'ow'}
        with self.assertRaises(ValueError):
            m.validate_state(s)


if __name__ == '__main__':
    unittest.main()
