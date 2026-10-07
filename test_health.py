"""Failure injection for durable health alerts; no network or real Discord sends."""
import copy
import unittest
import urllib.error
from datetime import datetime, timezone
import monitor_health as h


class HealthTests(unittest.TestCase):
    def state(self):
        state = {'version': 1, 'checked': 1, 'workflows': {}}
        h.upgrade_delivery(state)
        return state

    def event(self, state, component='apex', change='failed'):
        return h.enqueue(state, component, change, 'test notice', 100)

    def test_success_age_not_failure_age(self):
        now = 2000000000
        def run(age, conclusion):
            stamp = datetime.fromtimestamp(now-age, timezone.utc).isoformat()
            return dict(event='schedule', status='completed', conclusion=conclusion, created_at=stamp, updated_at=stamp)
        self.assertEqual(h.health([run(3600,'failure'),run(30*3600,'success')], now, now-40*3600), 'stale')
        self.assertEqual(h.health([run(3600,'failure'),run(4*3600,'success')], now, now-40*3600), 'waiting')
        self.assertIsNone(h.transition('failed', 'waiting'))
        self.assertEqual(h.transition('failed','ok'),'recovered')

    def test_disabled_and_consecutive_failures(self):
        self.assertEqual(h.health([],100,100,False),'disabled')
        stamp=datetime.fromtimestamp(99,timezone.utc).isoformat()
        runs=[dict(event='schedule',status='completed',conclusion=c,created_at=stamp,updated_at=stamp) for c in ('cancelled','timed_out')]
        self.assertEqual(h.health(runs,100,1),'failed')

    def test_success_persisted_and_no_repeat(self):
        state=self.state(); identity=self.event(state); saves=[]; sends=[]
        def save(): saves.append(copy.deepcopy(state))
        def send(text):
            self.assertIn(identity,saves[-1]['delivery_pending'])
            sends.append(text); return {'id':'123'}
        h.deliver_events(state,save,send)
        h.deliver_events(copy.deepcopy(state),save,send)
        self.assertEqual(len(sends),1)
        self.assertEqual(state['delivery_receipts'][identity]['message_id'],'123')
        self.assertFalse(state['delivery_pending']); self.assertFalse(state['delivery_queue'])

    def test_explicit_rejection_retries(self):
        state=self.state(); identity=self.event(state)
        def reject(text): raise urllib.error.HTTPError('redacted',429,'rate limit',{},None)
        h.deliver_events(state,lambda:None,reject)
        self.assertFalse(state['delivery_pending']); self.assertIn(identity,state['delivery_queue'])
        h.deliver_events(state,lambda:None,lambda text:{'id':'123'})
        self.assertIn(identity,state['delivery_receipts'])

    def test_unknown_does_not_retry_or_block_other_component(self):
        state=self.state(); a=self.event(state); b=self.event(state,'ow'); calls=[]
        def send(text):
            calls.append(text)
            if len(calls)==1: raise TimeoutError()
            return {'id':'456'}
        h.deliver_events(state,lambda:None,send)
        self.assertIn(a,state['delivery_pending']); self.assertIn(b,state['delivery_receipts'])
        c=self.event(state,'apex','recovered')
        h.deliver_events(state,lambda:None,send)
        self.assertEqual(len(calls),2); self.assertIn(c,state['delivery_queue'])

    def test_5xx_or_missing_receipt_remains_pending(self):
        for mode in ('5xx','missing'):
            state=self.state(); identity=self.event(state)
            def send(text):
                if mode=='5xx': raise urllib.error.HTTPError('redacted',503,'unavailable',{},None)
                return {}
            h.deliver_events(state,lambda:None,send)
            self.assertIn(identity,state['delivery_pending'])
            self.assertNotIn(identity,state['delivery_receipts'])

    def test_reservation_save_failure_prevents_post(self):
        state=self.state(); self.event(state); calls=[]
        def save(): raise OSError()
        with self.assertRaises(OSError): h.deliver_events(state,save,lambda text:calls.append(text))
        self.assertFalse(calls)

    def test_confirmation_save_failure_reload_holds(self):
        state=self.state(); identity=self.event(state); durable={}; calls=[]
        def save():
            if identity in state['delivery_receipts']: raise OSError()
            durable.clear(); durable.update(copy.deepcopy(state))
        with self.assertRaises(OSError): h.deliver_events(state,save,lambda text:(calls.append(text) or {'id':'123'}))
        h.deliver_events(durable,lambda:None,lambda text:calls.append(text))
        self.assertEqual(len(calls),1)

    def test_legacy_uncertain_is_never_erased(self):
        state={'version':1,'checked':99,'workflows':{'a':{'status':'failed'}},'delivery':'unconfirmed'}
        h.upgrade_delivery(state)
        evidence=copy.deepcopy(state['legacy_unconfirmed'])
        self.event(state,'other')
        h.deliver_events(state,lambda:None,lambda text:{'id':'123'})
        h.upgrade_delivery(state)
        self.assertEqual(evidence,state['legacy_unconfirmed'])
        self.assertFalse(state['delivery_queue'])


if __name__ == '__main__': unittest.main()
