/**
 * Base page entry point loaded by every Jinja template.
 */
import './api.js';
import './ui.js';

window.ApiReady = window.Api ? window.Api.init() : Promise.resolve();
