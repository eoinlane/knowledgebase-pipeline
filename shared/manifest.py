#!/usr/bin/env python3
"""
Pipeline manifest — end-to-end UUID tracking.

Tracks each recording through pipeline stages:
  arrive → transcribe → classify → speaker_id → reclassify → insights

SQLite DB at ~/audio-inbox/pipeline_manifest.db (Ubuntu) with WAL mode
for safe concurrent access from inotify watcher and watchdog timer.

Usage as CLI:
  python3 manifest.py arrive <uuid> <audio_path> <source>
  python3 manifest.py start <uuid> <stage>
  python3 manifest.py complete <uuid> <stage> <output_path>
  python3 manifest.py fail <uuid> <stage> <error_msg>
  python3 manifest.py summary
  python3 manifest.py stalled [--hours 2]
  python3 manifest.py failed
  python3 manifest.py trace <uuid>

Usage as module:
  from shared.manifest import Manifest
  m = Manifest()
  m.record_arrival(uuid, audio_path, "inotify")
  m.stage_start(uuid, "transcribe")
  m.stage_complete(uuid, "transcribe", "/path/to/output.txt")
"""

import os
import sqlite3
import sys
import time

MANIFEST_DB = os.path.expanduser("~/audio-inbox/pipeline_manifest.db")

VALID_STAGES = ("transcribe", "classify", "speaker_id", "reclassify", "insights")


class Manifest:
    def __init__(self, db_path=None):
        self.db_path = db_path or MANIFEST_DB
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS recordings (
                uuid TEXT PRIMARY KEY,
                audio_path TEXT NOT NULL,
                arrived_at TEXT NOT NULL,
                source TEXT,
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS pipeline_stages (
                uuid TEXT NOT NULL,
                stage TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'started',
                started_at TEXT,
                completed_at TEXT,
                error_msg TEXT,
                output_path TEXT,
                output_bytes INTEGER,
                PRIMARY KEY (uuid, stage)
            );

            CREATE INDEX IF NOT EXISTS idx_stages_status ON pipeline_stages(status);
            CREATE INDEX IF NOT EXISTS idx_recordings_completed ON recordings(completed_at);
        """)

    def record_arrival(self, uuid, audio_path, source="unknown"):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            "INSERT OR IGNORE INTO recordings (uuid, audio_path, arrived_at, source) VALUES (?, ?, ?, ?)",
            (uuid, audio_path, now, source)
        )
        self.conn.commit()

    def stage_start(self, uuid, stage):
        if stage not in VALID_STAGES:
            raise ValueError(f"Invalid stage: {stage}")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            """INSERT INTO pipeline_stages (uuid, stage, status, started_at)
               VALUES (?, ?, 'started', ?)
               ON CONFLICT(uuid, stage) DO UPDATE SET status='started', started_at=?, error_msg=NULL""",
            (uuid, stage, now, now)
        )
        self.conn.commit()

    def stage_complete(self, uuid, stage, output_path=None):
        """Record stage completion. Validates output file is non-empty."""
        if stage not in VALID_STAGES:
            raise ValueError(f"Invalid stage: {stage}")

        output_bytes = None
        if output_path:
            if not os.path.exists(output_path):
                self.stage_fail(uuid, stage, f"Output file missing: {output_path}")
                return False
            output_bytes = os.path.getsize(output_path)
            if output_bytes == 0:
                self.stage_fail(uuid, stage, f"Output file is 0 bytes: {output_path}")
                return False

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            """UPDATE pipeline_stages SET status='completed', completed_at=?, output_path=?, output_bytes=?
               WHERE uuid=? AND stage=?""",
            (now, output_path, output_bytes, uuid, stage)
        )

        # Check if all stages complete for this UUID
        done = self.conn.execute(
            "SELECT COUNT(*) FROM pipeline_stages WHERE uuid=? AND status='completed'",
            (uuid,)
        ).fetchone()[0]
        if done >= len(VALID_STAGES):
            self.conn.execute(
                "UPDATE recordings SET completed_at=? WHERE uuid=?", (now, uuid)
            )

        self.conn.commit()
        return True

    def stage_fail(self, uuid, stage, error_msg=""):
        if stage not in VALID_STAGES:
            raise ValueError(f"Invalid stage: {stage}")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            """INSERT INTO pipeline_stages (uuid, stage, status, started_at, error_msg)
               VALUES (?, ?, 'failed', ?, ?)
               ON CONFLICT(uuid, stage) DO UPDATE SET status='failed', error_msg=?""",
            (uuid, stage, now, error_msg[:500], error_msg[:500])
        )
        self.conn.commit()

    def get_stalled(self, max_hours=2):
        """UUIDs stuck in 'started' for longer than max_hours."""
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(time.time() - max_hours * 3600))
        rows = self.conn.execute(
            """SELECT ps.uuid, ps.stage, ps.started_at FROM pipeline_stages ps
               WHERE ps.status = 'started' AND ps.started_at < ?
               ORDER BY ps.started_at""",
            (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_failed(self):
        """UUIDs with failed stages (most recent first)."""
        rows = self.conn.execute(
            """SELECT uuid, stage, error_msg, started_at FROM pipeline_stages
               WHERE status = 'failed'
               ORDER BY started_at DESC LIMIT 20"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_incomplete(self):
        """UUIDs that arrived but haven't completed all stages."""
        rows = self.conn.execute(
            """SELECT r.uuid, r.arrived_at,
                      GROUP_CONCAT(ps.stage || ':' || ps.status, ', ') as stages
               FROM recordings r
               LEFT JOIN pipeline_stages ps ON r.uuid = ps.uuid
               WHERE r.completed_at IS NULL
               GROUP BY r.uuid
               ORDER BY r.arrived_at DESC
               LIMIT 20"""
        ).fetchall()
        return [dict(r) for r in rows]

    def trace(self, uuid):
        """Full trace of a single UUID through all stages."""
        rec = self.conn.execute(
            "SELECT * FROM recordings WHERE uuid=?", (uuid,)
        ).fetchone()
        stages = self.conn.execute(
            "SELECT * FROM pipeline_stages WHERE uuid=? ORDER BY started_at", (uuid,)
        ).fetchall()
        return {
            "recording": dict(rec) if rec else None,
            "stages": [dict(s) for s in stages]
        }

    def summary(self):
        """Pipeline summary stats."""
        total = self.conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]
        complete = self.conn.execute(
            "SELECT COUNT(*) FROM recordings WHERE completed_at IS NOT NULL"
        ).fetchone()[0]
        failed = self.conn.execute(
            "SELECT COUNT(DISTINCT uuid) FROM pipeline_stages WHERE status='failed'"
        ).fetchone()[0]
        stalled = len(self.get_stalled())
        return {
            "total": total,
            "complete": complete,
            "failed": failed,
            "stalled": stalled,
            "incomplete": total - complete
        }

    def close(self):
        self.conn.close()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Pipeline manifest CLI")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("arrive")
    p.add_argument("uuid")
    p.add_argument("audio_path")
    p.add_argument("source", nargs="?", default="unknown")

    p = sub.add_parser("start")
    p.add_argument("uuid")
    p.add_argument("stage")

    p = sub.add_parser("complete")
    p.add_argument("uuid")
    p.add_argument("stage")
    p.add_argument("output_path", nargs="?")

    p = sub.add_parser("fail")
    p.add_argument("uuid")
    p.add_argument("stage")
    p.add_argument("error_msg", nargs="?", default="")

    p = sub.add_parser("summary")

    p = sub.add_parser("stalled")
    p.add_argument("--hours", type=int, default=2)

    p = sub.add_parser("failed")

    p = sub.add_parser("incomplete")

    p = sub.add_parser("trace")
    p.add_argument("uuid")

    args = parser.parse_args()
    m = Manifest()

    if args.command == "arrive":
        m.record_arrival(args.uuid, args.audio_path, args.source)
        print(f"Recorded arrival: {args.uuid}")

    elif args.command == "start":
        m.stage_start(args.uuid, args.stage)
        print(f"Started {args.stage}: {args.uuid}")

    elif args.command == "complete":
        ok = m.stage_complete(args.uuid, args.stage, args.output_path)
        if ok:
            print(f"Completed {args.stage}: {args.uuid}")
        else:
            print(f"FAILED validation for {args.stage}: {args.uuid}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "fail":
        m.stage_fail(args.uuid, args.stage, args.error_msg)
        print(f"Recorded failure {args.stage}: {args.uuid}")

    elif args.command == "summary":
        s = m.summary()
        print(f"total={s['total']} complete={s['complete']} failed={s['failed']} stalled={s['stalled']} incomplete={s['incomplete']}")

    elif args.command == "stalled":
        rows = m.get_stalled(args.hours)
        if rows:
            for r in rows:
                print(f"  STALLED: {r['uuid']} at {r['stage']} since {r['started_at']}")
        else:
            print("No stalled recordings.")

    elif args.command == "failed":
        rows = m.get_failed()
        if rows:
            for r in rows:
                print(f"  FAILED: {r['uuid']} at {r['stage']} — {r['error_msg']}")
        else:
            print("No failed recordings.")

    elif args.command == "incomplete":
        rows = m.get_incomplete()
        if rows:
            for r in rows:
                print(f"  {r['uuid']} arrived {r['arrived_at']}: {r['stages'] or 'no stages started'}")
        else:
            print("All recordings complete.")

    elif args.command == "trace":
        t = m.trace(args.uuid)
        if not t["recording"]:
            print(f"UUID {args.uuid} not found in manifest.")
            sys.exit(1)
        rec = t["recording"]
        print(f"UUID: {rec['uuid']}")
        print(f"Arrived: {rec['arrived_at']} via {rec['source']}")
        print(f"Complete: {rec['completed_at'] or 'NOT YET'}")
        for s in t["stages"]:
            status = s['status'].upper()
            extra = f" ({s['output_bytes']}B)" if s.get('output_bytes') else ""
            extra += f" — {s['error_msg']}" if s.get('error_msg') else ""
            print(f"  {s['stage']:15s} {status:10s} {s.get('completed_at') or s.get('started_at', '')}{extra}")

    else:
        parser.print_help()

    m.close()


if __name__ == "__main__":
    main()
