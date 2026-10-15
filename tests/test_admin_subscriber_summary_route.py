import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from starlette.testclient import TestClient
import app as web_app
from account_service import AccountService
from auth_service import GoogleAuthService

class AdminSubscriberSummaryRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.auth=GoogleAuthService(client_id='synthetic',client_secret='synthetic',session_secret='synthetic-session-signing-material-long-enough',data_dir=root)
        self.accounts=AccountService(root,admin_emails='admin@example.test')
        for name,value in [('google_auth',self.auth),('account_service',self.accounts),('rate_limiter',web_app.FixedWindowRateLimiter())]:
            p=patch.object(web_app,name,value);p.start();self.addCleanup(p.stop)
        self.client=TestClient(web_app.app,base_url='https://gumruksor.com');self.addCleanup(self.client.close)
        self.start='2026-10-14T04:00:00.000Z';self.end='2026-10-15T04:00:00.000Z'
        a,b=[datetime.fromisoformat(x.replace('Z','+00:00')).timestamp() for x in [self.start,self.end]]
        with sqlite3.connect(self.accounts.db_path) as db:
            for i,at in enumerate([a-1,a,a+1,b]):db.execute("INSERT INTO users(google_sub,email,name,picture,created_at,last_login_at) VALUES(?,?,'Fixture','',?,?)",(str(i),f'fixture{i}@example.test',at,at))
    def request(self,email=None,query=None):
        headers={'Origin':'https://gumruksor.com'}
        if email:headers['Cookie']=f'{self.auth.session_cookie}='+self.auth.create_session({'sub':'test-actor','email':email})
        return self.client.get('/api/admin/subscriber-summary',params=query or {'start':self.start,'end':self.end},headers=headers)
    def test_existing_gate_denies_anonymous_and_ordinary_users_before_counting(self):
        with patch.object(self.accounts,'admin_subscriber_summary',side_effect=AssertionError('Must not read')) as count:
            self.assertEqual(self.request().status_code,403)
            self.assertEqual(self.request('ordinary@example.test').status_code,403)
            count.assert_not_called()
    def test_authorized_aggregate_is_private_and_has_no_user_rows_or_fake_duration(self):
        response=self.request('admin@example.test');self.assertEqual(response.status_code,200,response.text)
        data=response.json();self.assertEqual(data['registeredAccounts']['total'],4);self.assertEqual(data['registeredAccounts']['new'],2)
        self.assertIsNone(data['paidSubscribers']['active']);self.assertIsNone(data['usage']['totalMinutes'])
        self.assertNotIn('@example.test',response.text);self.assertEqual(response.headers['cache-control'],'private, no-store')
    def test_invalid_period_is_rejected(self):
        self.assertEqual(self.request('admin@example.test',{'start':'bad','end':'bad'}).status_code,400)
