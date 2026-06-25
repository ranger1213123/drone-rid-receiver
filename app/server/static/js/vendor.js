/**
 * Vendor bundle entry — exposes bundled third-party libraries to legacy
 * inline page scripts while keeping supply-chain assets local.
 */
import Alpine from 'alpinejs';
import { io } from 'socket.io-client';

window.Alpine = Alpine;
window.io = io;

Alpine.start();
