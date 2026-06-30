# -*- coding: utf-8 -*-
import sys, time, threading, json
sys.path.insert(0, '/opt/rid-receiver')
from server_web import db, alert_sys, process_drone_data, mock_drone_data

print 'DB ready, lines:', len(db.get_power_lines())

t = time.time()
drone = mock_drone_data("DRONE-X", 39.9042, 116.4074, 150, t)
print 'Mock data keys:', drone.keys()

process_drone_data(drone)
print 'After process...'
print 'Drones:', db.get_active_drones()
print 'Stats:', db.get_stats()
