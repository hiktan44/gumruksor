import sqlite3
import unittest
from datetime import datetime
from subscriber_reporting import account_summary, report_period

class SubscriberReportingTests(unittest.TestCase):
    def test_exact_period_and_private_aggregate(self):
        start, end = '2026-10-14T04:00:00.000Z', '2026-10-15T04:00:00.000Z'
        a, b = [datetime.fromisoformat(x.replace('Z', '+00:00')).timestamp() for x in [start, end]]
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE users(created_at INTEGER, email TEXT)')
            db.executemany('INSERT INTO users VALUES (?,?)', [(t, 'synthetic@example.test') for t in [a-1,a,a+1,b]])
            before = db.total_changes
            summary = account_summary(db, start, end)
            self.assertEqual(summary['registeredAccounts']['total'], 4)
            self.assertEqual(summary['registeredAccounts']['new'], 2)
            self.assertIsNone(summary['paidSubscribers']['active'])
            self.assertIsNone(summary['usage']['sessions'])
            self.assertNotIn('synthetic@example.test', str(summary))
            self.assertEqual(db.total_changes, before)
    def test_empty_known_dataset_is_zero_accounts_but_unknown_usage(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE users(created_at INTEGER)')
            summary=account_summary(db,'2026-10-14T00:00:00Z','2026-10-15T00:00:00Z')
            self.assertEqual(summary['registeredAccounts']['new'],0)
            self.assertIsNone(summary['usage']['totalMinutes'])
    def test_invalid_periods(self):
        for start,end in [('2026-01-01','2026-01-02'),('2026-01-01T00:00:00','2026-01-02T00:00:00'),('2026-01-01T00:00:00Z','2026-03-01T00:00:00Z')]:
            with self.subTest(start=start),self.assertRaises(ValueError):report_period(start,end)

if __name__=='__main__':unittest.main()
