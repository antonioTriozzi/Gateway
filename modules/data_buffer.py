import sqlite3
import json
from datetime import datetime, timezone
from typing import List, Dict, Any


def utc_timestamp_iso() -> str:
    """UTC esplicito (SQLite CURRENT_TIMESTAMP è UTC ma senza 'Z' confonde parser/UI)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

class DataBuffer:
    def __init__(self, db_path: str = "buffer.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row 
        
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        
        self.create_table()

    def create_table(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_name TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    data TEXT NOT NULL
                )
            """)

    def prune_old_records(self, days_to_keep: int = 7):
        """
        Rimuove i record più vecchi di X giorni per evitare che il DB riempia la SD.
        Da chiamare periodicamente (es. all'avvio o una volta al giorno).
        """
        with self.conn:
            deleted = self.conn.execute(
                "DELETE FROM readings WHERE timestamp < date('now', ?)",
                (f'-{days_to_keep} days',)
            ).rowcount
            if deleted > 0:
                print(f"INFO: Pruned {deleted} old records from buffer.")

    def save_readings(self, device_name: str, readings: List[Dict[str, Any]]):
        ts = utc_timestamp_iso()
        with self.conn:
            self.conn.execute(
                "INSERT INTO readings (device_name, timestamp, data) VALUES (?, ?, ?)",
                (device_name, ts, json.dumps(readings)),
            )

    def get_pending_readings(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Recupera i record più vecchi non ancora inviati.
        """
        with self.conn:
            cursor = self.conn.execute(
                "SELECT id, device_name, timestamp, data FROM readings ORDER BY timestamp ASC LIMIT ?",
                (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def delete_readings_batch(self, record_ids: List[int]):
        """
        Cancella un batch di record dopo l'invio confermato.
        """
        if not record_ids:
            return
        placeholders = ', '.join('?' for _ in record_ids)
        with self.conn:
            self.conn.execute(f"DELETE FROM readings WHERE id IN ({placeholders})", record_ids)

    def count_pending(self) -> int:
        with self.conn:
            cursor = self.conn.execute("SELECT COUNT(*) FROM readings")
            return cursor.fetchone()[0]