import tempfile
import unittest
from pathlib import Path

from scenic_reservation.storage import Database


class StorageTest(unittest.TestCase):
    def test_transaction_rolls_back_on_error(self):
        database = Database()
        with self.assertRaises(RuntimeError):
            with database.transaction():
                database.connection.execute(
                    "INSERT INTO entrances(entrance_id,name,created_at) VALUES('e1','北门','now')"
                )
                raise RuntimeError("停止事务")
        count = database.connection.execute("SELECT COUNT(*) FROM entrances").fetchone()[0]
        self.assertEqual(0, count)
        database.close()

    def test_schema_enables_foreign_keys(self):
        database = Database()
        enabled = database.connection.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(1, enabled)
        database.close()

    def test_ledger_unique_index_blocks_double_booking_same_item(self):
        database = Database()
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO entrances(entrance_id,name,created_at) VALUES('e1','北门','now')"
            )
            connection.execute(
                "INSERT INTO slot_capacity(entrance_id,visit_date,slot,capacity,created_at,updated_at) "
                "VALUES('e1','2026-10-08','09:00-12:00',5,'t','t')"
            )
            connection.execute(
                "INSERT INTO capacity_ledger(entrance_id,visit_date,slot,item_id,order_id,"
                "request_id,direction,reason,delta,balance_after,occurred_at) "
                "VALUES('e1','2026-10-08','09:00-12:00','i1','o1','r1','hold','reserved',1,1,'t')"
            )
            with self.assertRaises(Exception):
                connection.execute(
                    "INSERT INTO capacity_ledger(entrance_id,visit_date,slot,item_id,order_id,"
                    "request_id,direction,reason,delta,balance_after,occurred_at) "
                    "VALUES('e1','2026-10-08','09:00-12:00','i1','o1','r1','hold','reserved',1,2,'t')"
                )
        database.close()

    def test_schema_persists_to_disk_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.sqlite3"
            database = Database(path)
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO entrances(entrance_id,name,created_at) VALUES('e1','北门','now')"
                )
            database.close()
            reopened = Database(path)
            count = reopened.connection.execute("SELECT COUNT(*) FROM entrances").fetchone()[0]
            self.assertEqual(1, count)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
