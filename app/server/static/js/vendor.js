/**
 * Vendor bundle entry — exposes bundled third-party libraries to legacy
 * inline page scripts while keeping supply-chain assets local.
 */
import Alpine from 'alpinejs';
import Chart from 'chart.js/auto';
import * as L from 'leaflet';
import { io } from 'socket.io-client';
import 'leaflet/dist/leaflet.css';

window.Alpine = Alpine;
window.Chart = Chart;
window.L = L;
window.io = io;

Alpine.start();
