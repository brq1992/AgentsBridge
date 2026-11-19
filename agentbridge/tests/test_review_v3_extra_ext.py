import json
import unittest
import test_review_v2_ext as fixture

class ResponseMeaning(unittest.TestCase):
    setUp = fixture.Boundaries.setUp
    save_parent = fixture.Boundaries.save_parent

    def queue(self):
        with self.store.connect() as c:
            c.execute("INSERT INTO net_jobs(id,envelope,state,auto_ack) VALUES(?,?,'submitted','queued')",('reply',json.dumps(self.m)))
            rid=self.a._enqueue(c,'ack',dict(message_id='reply',actor=self.m['receiver'],generation=self.s['generation'],context={'auto_ack':'agent'}))
            return dict(c.execute('SELECT * FROM net_ops WHERE id=?',(rid,)).fetchone())

    def test_rpc_ok_with_expired_denial_is_not_received(self):
        op=self.queue()
        with self.store.connect() as c:
            self.a._resolve_auto_ack(c,op,{'ok':True,'result':{'should_execute':False,'reason':'expired'}})
            row=c.execute("SELECT state,auto_ack FROM net_jobs WHERE id='reply'").fetchone()
        self.assertNotEqual(tuple(row),('received','agent_received'))

    def test_rpc_ok_with_revoked_denial_is_not_received(self):
        op=self.queue()
        with self.store.connect() as c:
            self.a._resolve_auto_ack(c,op,{'ok':True,'result':{'should_execute':False,'reason':'binding_revoked'}})
            row=c.execute("SELECT state,auto_ack FROM net_jobs WHERE id='reply'").fetchone()
        self.assertNotEqual(tuple(row),('received','agent_received'))

    def test_status_exposes_agent_receipt(self):
        op=self.queue()
        with self.store.connect() as c:
            self.a._resolve_auto_ack(c,op,{'ok':True,'result':{'should_execute':False,'kind':'reply'}})
        job=next(j for j in self.a.status()['jobs'] if j['id']=='reply')
        self.assertEqual(job.get('auto_ack'),'agent_received')

if __name__=='__main__': unittest.main()
