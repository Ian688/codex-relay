"""Actual subprocess JSON-RPC smoke test with a fake offline Codex server."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from codex_autoresume.protocol import AppServer

MOCK = r'''#!/usr/bin/env python3
import sys, json
for raw in sys.stdin:
    msg=json.loads(raw)
    method=msg.get('method')
    if 'id' not in msg: continue
    if method=='initialize': result={'serverInfo': {'name':'mock','version':'1'}}
    elif method=='account/rateLimits/read':
        result={'accountId':'acct-A','ordinaryUsageAllowed':True,'rateLimits':{'primary':{'usedPercent':1,'resetsAt':2100000000}}}
    elif method=='thread/list': result={'data':[{'id':'s1','status':'notLoaded','updatedAt':2000000000}], 'nextCursor':None}
    elif method=='thread/read': result={'thread':{'id':'s1','status':'notLoaded','turns':[{'id':'t1','status':'failed','error':{'codexErrorInfo':'usageLimitExceeded'}}]}}
    elif method=='large/read': result={'text':'x' * 200000}
    elif method=='thread/resume': result={'thread':{'id':'s1','status':'idle'}}
    elif method=='turn/start': result={'turn':{'id':'new-turn','status':'inProgress'}}
    else: result={}
    print(json.dumps({'id':msg['id'],'result':result}), flush=True)
    if method=='turn/start':
        print(json.dumps({'method':'turn/completed','params':{'threadId':'s1','turn':{'id':'new-turn','status':'completed'}}}), flush=True)
'''

class ProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_request_response_and_notification(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'codex'
            p.write_text(MOCK)
            p.chmod(0o755)
            client = AppServer(str(p))
            try:
                await client.start()
                self.assertTrue((await client.limits())["ordinaryUsageAllowed"])
                self.assertEqual(len((await client.request("large/read"))["text"]), 200000)
                self.assertEqual(len(await client.list_threads()), 1)
                self.assertEqual((await client.read_thread("s1"))["turns"][-1]["id"], "t1")
                self.assertEqual((await client.resume_thread("s1"))["thread"]["status"], "idle")
                self.assertEqual((await client.start_turn("s1", "继续"))["turn"]["id"], "new-turn")
                notification = await asyncio.wait_for(client.notifications.get(), 5)
                self.assertEqual(notification["method"], "turn/completed")
            finally:
                await client.close()

if __name__ == "__main__": unittest.main()
