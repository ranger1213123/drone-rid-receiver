# -*- coding: utf-8 -*-
import sqlite3, os

db_path = '/opt/rid-receiver/data/rid.db'
d = os.path.dirname(db_path)
if d and not os.path.exists(d):
    os.makedirs(d)

conn = sqlite3.connect(db_path)
c = conn.cursor()
c.executescript('''
    CREATE TABLE IF NOT EXISTS power_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        lat1 REAL, lon1 REAL, alt1 REAL DEFAULT 0,
        lat2 REAL, lon2 REAL, alt2 REAL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        drone_id TEXT, level TEXT, distance REAL,
        line_id INTEGER, line_name TEXT,
        sms_pilot INTEGER DEFAULT 0, sms_staff INTEGER DEFAULT 0,
        message TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS trajectories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        drone_id TEXT, lat REAL, lon REAL, alt REAL,
        distance_to_line REAL, line_id INTEGER,
        recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS drones (
        drone_id TEXT PRIMARY KEY,
        mac TEXT, name TEXT,
        lat REAL DEFAULT 0, lon REAL DEFAULT 0, alt REAL DEFAULT 0,
        speed REAL DEFAULT 0, heading REAL DEFAULT 0,
        rssi INTEGER DEFAULT 0, min_distance REAL DEFAULT 999999,
        nearest_line_id INTEGER, status TEXT DEFAULT 'active',
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
''')

lines = [
    ('test1', 39.9042, 116.4074, 50, 39.9142, 116.4274, 55),
    ('test2', 39.8942, 116.3974, 45, 39.9042, 116.4174, 48),
]
for name, l1, o1, a1, l2, o2, a2 in lines:
    c.execute('INSERT OR IGNORE INTO power_lines (name,lat1,lon1,alt1,lat2,lon2,alt2) VALUES (?,?,?,?,?,?,?)',
              (name, l1, o1, a1, l2, o2, a2))

conn.commit()
conn.close()
print 'init ok, lines: %d' % len(lines)
