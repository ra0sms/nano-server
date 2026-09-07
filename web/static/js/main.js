let relayNames = Array(16).fill().map((_, i) => 'Relay ' + (i+1));
let relayState = Array(16).fill(0);
let relayMode = ['toggle', 'toggle'];
let pttActive = false;

function showToast(msg, isOk = true) {
    const toast = document.getElementById('toast');
    toast.textContent = msg;
    toast.style.background = isOk ? '#1faa59' : '#d64545';
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 2000);
}

// Tab switching
document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', function() {
        const tabName = this.dataset.tab;
        if (!tabName) return;

        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        this.classList.add('active');

        document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
        document.getElementById(`${tabName}-panel`).classList.add('active');

        if (tabName === 'trx') loadTrxState();
    });
});

// PTT indicator
function updatePttIndicator() {
    const el = document.getElementById('ptt-indicator');
    if (!el) return;
    if (pttActive) {
        el.textContent = '● PTT: ON';
        el.style.background = '#d64545';
        el.style.color = 'white';
    } else {
        el.textContent = '● PTT: OFF';
        el.style.background = '#2a2d34';
        el.style.color = '#7f8c8d';
    }
}

// Relay functions
function renderRelays() {
    const container1 = document.getElementById('relays1');
    const container2 = document.getElementById('relays2');
    container1.innerHTML = '';
    container2.innerHTML = '';

    for (let i = 0; i < 16; i++) {
        const btn = document.createElement('button');
        btn.className = 'relay-btn' + (relayState[i] ? ' on' : '');
        btn.textContent = relayNames[i];
        btn.onclick = () => toggleRelay(i);
        if (pttActive) {
            btn.style.opacity = '0.5';
            btn.style.cursor = 'not-allowed';
            btn.title = '🔒 PTT active — relay switching blocked';
        }
        if (i < 8) container1.appendChild(btn);
        else container2.appendChild(btn);
    }
    updatePttIndicator();
}

function loadRelays() {
    fetch('/state')
        .then(r => r.json())
        .then(data => {
            relayState = data.state;
            relayNames = data.names;
            relayMode = data.mode;
            document.getElementById('mode0_label').textContent = relayMode[0];
            document.getElementById('mode1_label').textContent = relayMode[1];
            renderRelays();

            // Settings panel
            document.getElementById('group1_mode').value = relayMode[0];
            document.getElementById('group2_mode').value = relayMode[1];

            const names1 = document.getElementById('relay-names1');
            const names2 = document.getElementById('relay-names2');
            names1.innerHTML = '';
            names2.innerHTML = '';

            for (let i = 0; i < 16; i++) {
                const div = document.createElement('div');
                div.className = 'name-row';
                div.innerHTML = `<span>${i+1}.</span><input type="text" id="relay_name_${i}" value="${relayNames[i]}">`;
                if (i < 8) names1.appendChild(div);
                else names2.appendChild(div);
            }
        });
}

function checkPttStatus() {
    return fetch('/ptt/status')
        .then(r => r.json())
        .then(data => {
            pttActive = data.active;
            updatePttIndicator();
        })
        .catch(() => {});
}

function toggleRelay(idx) {
    if (pttActive) {
        showToast('🔒 PTT active — relay switching blocked', false);
        return;
    }
    fetch(`/toggle/${idx}`)
        .then(r => r.json())
        .then(data => {
            relayState = data.state;
            renderRelays();
            showToast(`Toggled ${relayNames[idx]}`, true);
        })
        .catch(() => showToast('Failed to toggle relay', false));
}

function saveRelaySettings() {
    const newNames = [];
    for (let i = 0; i < 16; i++) {
        const input = document.getElementById(`relay_name_${i}`);
        if (input) newNames.push(input.value);
        else newNames.push(relayNames[i]);
    }

    const data = {
        names: newNames,
        mode: [
            document.getElementById('group1_mode').value,
            document.getElementById('group2_mode').value
        ]
    };

    fetch('/settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
    }).then(r => {
        if (r.ok) {
            showToast('✅ Relay settings saved successfully!', true);
            loadRelays();
        } else {
            showToast('❌ Failed to save relay settings', false);
        }
    }).catch(() => showToast('❌ Network error while saving', false));
}

// TRX functions
function loadTrxState() {
    fetch('/trx/state')
        .then(r => r.json())
        .then(data => {
            const statusDiv = document.getElementById('trx-status');
            const freqDiv = document.getElementById('trx-freq');
            const bandDiv = document.getElementById('trx-band');
            const modeDiv = document.getElementById('trx-mode');

            if (data.online) {
                statusDiv.innerHTML = '🟢 ONLINE';
                statusDiv.className = 'status-online';
                freqDiv.textContent = (data.freq / 1000000).toFixed(6) + ' MHz';
            } else {
                statusDiv.innerHTML = '🔴 OFFLINE';
                statusDiv.className = 'status-offline';
                freqDiv.textContent = '---.--- MHz';
            }
            bandDiv.textContent = 'Band: ' + data.band;
            modeDiv.textContent = 'Mode: ' + data.mode;
        })
        .catch(() => {
            document.getElementById('trx-status').innerHTML = '🔴 OFFLINE';
        });
}

// TRX Control Functions
function freqStep(step) {
    fetch('/trx/freq_step', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({step: step})
    })
    .then(r => r.json())
    .then(data => {
        const stepLabel = step >= 0 ? '+' + step : '' + step;
        showToast('📡 Freq: ' + (data.freq / 1000000).toFixed(6) + ' MHz (' + stepLabel + ' Hz)', true);
        loadTrxState();
    })
    .catch(() => showToast('❌ Failed to change frequency', false));
}

function setBand(band) {
    fetch('/trx/set_band', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({band: band})
    })
    .then(r => {
        if (!r.ok) throw new Error('Band not found');
        return r.json();
    })
    .then(data => {
        showToast('📡 Switched to ' + data.band + ' (' + (data.freq / 1000000).toFixed(6) + ' MHz)', true);
        loadTrxState();
    })
    .catch(() => showToast('❌ Failed to switch band', false));
}

// Populate the TRX serial port/config selects from shared response data.
function populateTrxConfig(cfg, ports) {
    const select = document.getElementById('trx-port');
    select.innerHTML = '<option value="">-- Select port --</option>';
    (ports || []).forEach(p => {
        const opt = document.createElement('option');
        opt.value = p;
        opt.textContent = p;
        select.appendChild(opt);
    });
    // Set port if it exists in the list, otherwise add it as an option
    if (cfg.serial_port && (ports || []).includes(cfg.serial_port)) {
        select.value = cfg.serial_port;
    } else if (cfg.serial_port) {
        const opt = document.createElement('option');
        opt.value = cfg.serial_port;
        opt.textContent = cfg.serial_port + ' (current)';
        opt.selected = true;
        select.appendChild(opt);
    }
    document.getElementById('trx-baudrate').value = cfg.baudrate;
    document.getElementById('trx-protocol').value = cfg.protocol;
    document.getElementById('trx-enabled').value = cfg.enabled;
    document.getElementById('trx-uart1-enabled').checked = cfg.uart1_enabled !== false;
    document.getElementById('trx-radio-addr').value = '0x' + cfg.radio_addr.toString(16).toUpperCase().padStart(2, '0');
}

function loadTrxConfig() {
    // Load available ports
    fetch('/trx/ports')
        .then(r => r.json())
        .then(ports => {
            document.getElementById('trx-port').innerHTML =
                '<option value="">-- Scanning... --</option>';
            return fetch('/trx/config')
                .then(r => r.json())
                .then(cfg => ({cfg, ports}));
        })
        .then(({cfg, ports}) => {
            populateTrxConfig(cfg, ports);
        })
        .catch(() => {
            // Fallback: load config without port list
            fetch('/trx/config')
                .then(r => r.json())
                .then(cfg => populateTrxConfig(cfg, null));
        });
}

function saveTrxSettings() {
    // Validate transceiver address
    const addrStr = document.getElementById('trx-radio-addr').value.trim();
    let radioAddr;
    if (!addrStr) {
        showToast('❌ Transceiver address is required', false);
        return;
    }
    const addrMatch = addrStr.match(/^0x([0-9a-fA-F]{1,2})$/);
    if (!addrMatch) {
        showToast('❌ Invalid address format. Use hex format: 0x00-0xFF', false);
        return;
    }
    radioAddr = parseInt(addrStr, 16);
    if (isNaN(radioAddr) || radioAddr < 0 || radioAddr > 255) {
        showToast('❌ Address must be between 0x00 and 0xFF', false);
        return;
    }

    // Validate serial port
    const port = document.getElementById('trx-port').value;
    if (!port) {
        showToast('❌ Select a serial port', false);
        return;
    }

    const data = {
        serial_port: port,
        baudrate: parseInt(document.getElementById('trx-baudrate').value),
        protocol: document.getElementById('trx-protocol').value,
        radio_addr: radioAddr,
        enabled: document.getElementById('trx-enabled').value === 'true',
        uart1_enabled: document.getElementById('trx-uart1-enabled').checked
    };

    fetch('/trx/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
    }).then(r => {
        if (r.ok) {
            showToast('✅ TRX settings saved successfully!', true);
        } else {
            return r.text().then(t => showToast('❌ ' + t, false));
        }
    }).catch(() => showToast('❌ Network error while saving', false));
}

function saveAllSettings() {
    saveRelaySettings();
    saveTrxSettings();
    showToast('💾 Saving all settings...', true);
}

// TRX port management functions
function refreshPorts() {
    const select = document.getElementById('trx-port');
    const currentVal = select.value;
    select.innerHTML = '<option value="">-- Scanning... --</option>';
    fetch('/trx/ports')
        .then(r => r.json())
        .then(ports => {
            select.innerHTML = '<option value="">-- Select port --</option>';
            ports.forEach(p => {
                const opt = document.createElement('option');
                opt.value = p;
                opt.textContent = p;
                select.appendChild(opt);
            });
            // Restore previous selection if still available
            if (currentVal && ports.includes(currentVal)) {
                select.value = currentVal;
            }
            showToast('🔍 Found ' + ports.length + ' port(s)', true);
        })
        .catch(() => {
            select.innerHTML = '<option value="">-- Scan failed --</option>';
            showToast('❌ Failed to scan ports', false);
        });
}

function reconnectTrx() {
    const port = document.getElementById('trx-port').value;
    if (!port) {
        showToast('❌ Select a port first', false);
        return;
    }
    showToast('🔄 Reconnecting to ' + port + '...', true);
    fetch('/trx/reinit', {method: 'POST'})
        .then(r => r.json())
        .then(data => {
            if (data.status === 'ok') {
                showToast('✅ Connected to ' + port, true);
                loadTrxState();
            } else {
                showToast('❌ ' + (data.message || 'Connection failed'), false);
            }
        })
        .catch(() => showToast('❌ Reconnect failed', false));
}

function onPortChange() {
    const port = document.getElementById('trx-port').value;
    if (port) {
        // Auto-save the port change and reconnect
        const data = {
            serial_port: port,
            baudrate: parseInt(document.getElementById('trx-baudrate').value),
            protocol: document.getElementById('trx-protocol').value,
            radio_addr: parseInt(document.getElementById('trx-radio-addr').value.trim(), 16) || 0x70,
            enabled: document.getElementById('trx-enabled').value === 'true',
            uart1_enabled: document.getElementById('trx-uart1-enabled').checked
        };
        fetch('/trx/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        }).then(r => {
            if (r.ok) {
                // Now reconnect with the new port
                fetch('/trx/reinit', {method: 'POST'})
                    .then(r => r.json())
                    .then(result => {
                        if (result.status === 'ok') {
                            showToast('✅ Switched to ' + port, true);
                            loadTrxState();
                        } else {
                            showToast('⚠️ Port saved but connection failed: ' + (result.message || ''), false);
                        }
                    })
                    .catch(() => showToast('⚠️ Port saved, reconnect manually', false));
            }
        });
    }
}

// Camera functions
let cameraVisible = false;

function toggleCamera() {
    const container = document.getElementById('camera-container');
    const btn = document.querySelector('.camera-btn');
    if (cameraVisible) {
        container.innerHTML = '';
        if (btn) btn.textContent = 'Show Camera';
        showToast('Camera hidden', true);
    } else {
        container.innerHTML = '<img src="/stream" style="max-width: 100%; border-radius: 10px;">';
        if (btn) btn.textContent = 'Hide Camera';
        showToast('Camera shown', true);
    }
    cameraVisible = !cameraVisible;
}

function openCameraWindow() {
    window.open('/camera', '_blank');
    showToast('Opening camera in new window', true);
}

// Auto-refresh TRX state every 2 seconds when TRX tab is active
setInterval(() => {
    const activePanel = document.querySelector('.panel.active');
    if (activePanel && activePanel.id === 'trx-panel') {
        loadTrxState();
    }
}, 2000);

// ================= Audio functions =================
function updateSpeakerVal() {
    const s = document.getElementById('speaker-slider');
    document.getElementById('speaker-val').textContent = s.value + '%';
}
function updateMicVal() {
    const m = document.getElementById('mic-slider');
    document.getElementById('mic-val').textContent = m.value + '%';
}

function loadAudioState() {
    fetch('/audio/state')
        .then(r => r.json())
        .then(data => {
            document.getElementById('speaker-slider').value = data.speaker;
            document.getElementById('speaker-val').textContent = data.speaker + '%';
            document.getElementById('mic-slider').value = data.mic;
            document.getElementById('mic-val').textContent = data.mic + '%';
        })
        .catch(() => {});
}

// Attach live value update on slider input
document.addEventListener('DOMContentLoaded', function() {
    const sp = document.getElementById('speaker-slider');
    const mc = document.getElementById('mic-slider');
    if (sp) sp.addEventListener('input', updateSpeakerVal);
    if (mc) mc.addEventListener('input', updateMicVal);
});

function setSpeaker() {
    const val = document.getElementById('speaker-slider').value;
    fetch('/audio/speaker', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({volume: parseInt(val)})
    }).then(r => {
        if (r.ok) showToast('✅ Speaker volume set to ' + val + '%', true);
        else showToast('❌ Failed to set speaker', false);
    }).catch(() => showToast('❌ Network error', false));
}

function setMic() {
    const val = document.getElementById('mic-slider').value;
    fetch('/audio/mic', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({volume: parseInt(val)})
    }).then(r => {
        if (r.ok) showToast('✅ Mic capture set to ' + val + '%', true);
        else showToast('❌ Failed to set mic', false);
    }).catch(() => showToast('❌ Network error', false));
}

// ================= Config functions =================
function loadConfig() {
    fetch('/config/data')
        .then(r => r.json())
        .then(data => {
            document.getElementById('server-ip-input').value = data.server_ip;
            document.getElementById('client-ip-input').value = data.client_ip;
            document.getElementById('audio-rate').value = data.audio_rate;
            document.getElementById('audio-buffer').value = data.audio_buffer;

            // Profiles
            const grid = document.getElementById('profile-grid');
            grid.innerHTML = '';
            data.profiles.forEach(name => {
                const card = document.createElement('div');
                card.className = 'profile-card';
                card.innerHTML = `
                    <span class="profile-name">${escapeHtml(name)}</span>
                    <div class="profile-actions">
                        <button class="btn-small btn-success" onclick="loadProfile('${name}')">Load</button>
                        <button class="btn-small btn-danger" onclick="deleteProfile('${name}')">Del</button>
                    </div>
                `;
                grid.appendChild(card);
            });
            // Empty slot
            if (data.profiles.length < 5) {
                const slot = document.createElement('div');
                slot.className = 'profile-card';
                slot.style.borderStyle = 'dashed';
                slot.innerHTML = `
                    <span class="profile-name" style="color:#666;">Empty Slot</span>
                    <input type="text" id="new-profile-name" placeholder="Profile Name" style="width:100%;padding:5px;margin-bottom:5px;background:#2a2d34;color:white;border:1px solid #444;border-radius:4px;" maxlength="15">
                    <button class="btn-small btn-success" onclick="saveProfile()">Save Current</button>
                `;
                grid.appendChild(slot);
            }
        })
        .catch(() => {});
}

function saveServerIp() {
    const ip = document.getElementById('server-ip-input').value.trim();
    fetch('/config/server_ip', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ip: ip})
    }).then(r => {
        if (r.ok) showToast('✅ Server IP saved', true);
        else return r.text().then(t => { showToast('❌ ' + t, false); });
    }).catch(() => showToast('❌ Network error', false));
}

function saveClientIp() {
    const ip = document.getElementById('client-ip-input').value.trim();
    fetch('/config/client_ip', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ip: ip})
    }).then(r => {
        if (r.ok) showToast('✅ Client IP saved', true);
        else return r.text().then(t => { showToast('❌ ' + t, false); });
    }).catch(() => showToast('❌ Network error', false));
}

function saveAudioSettings() {
    const data = {
        rate: parseInt(document.getElementById('audio-rate').value),
        buffer: parseInt(document.getElementById('audio-buffer').value)
    };
    fetch('/config/audio', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
    }).then(r => {
        if (r.ok) showToast('✅ Audio settings saved', true);
        else showToast('❌ Failed to save audio settings', false);
    }).catch(() => showToast('❌ Network error', false));
}

function saveProfile() {
    const name = document.getElementById('new-profile-name');
    if (!name || !name.value.trim()) {
        showToast('❌ Enter a profile name', false);
        return;
    }
    fetch('/config/save_profile', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: name.value.trim()})
    }).then(r => r.json()).then(data => {
        if (data.success) {
            showToast('✅ ' + data.message, true);
            loadConfig();
        } else {
            showToast('❌ ' + data.message, false);
        }
    }).catch(() => showToast('❌ Network error', false));
}

function loadProfile(name) {
    fetch('/config/load_profile', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: name})
    }).then(r => r.json()).then(data => {
        if (data.success) {
            showToast('✅ ' + data.message, true);
            loadConfig();
        } else {
            showToast('❌ ' + data.message, false);
        }
    }).catch(() => showToast('❌ Network error', false));
}

function deleteProfile(name) {
    if (!confirm('Delete profile ' + name + '?')) return;
    fetch('/config/delete_profile', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: name})
    }).then(r => r.json()).then(data => {
        if (data.success) {
            showToast('✅ ' + data.message, true);
            loadConfig();
        } else {
            showToast('❌ ' + data.message, false);
        }
    }).catch(() => showToast('❌ Network error', false));
}

function restartServices() {
    if (!confirm('Restart audio services?')) return;
    fetch('/config/restart_services', {method: 'POST'})
        .then(r => {
            if (r.ok) showToast('✅ Services restarted', true);
            else showToast('❌ Failed to restart', false);
        }).catch(() => showToast('❌ Network error', false));
}

function restartWebPanel() {
    if (!confirm('Restart the web panel? The page will reload after restart.')) return;
    fetch('/config/restart_web', {method: 'POST'})
        .then(r => {
            if (r.ok) {
                showToast('🔄 Web panel restarting...', true);
                setTimeout(() => { location.reload(); }, 3000);
            } else {
                showToast('❌ Failed to restart web panel', false);
            }
        }).catch(() => showToast('❌ Network error', false));
}

// ================= Status functions =================
function loadLocalIp() {
    fetch('/status/local_ip')
        .then(r => r.json())
        .then(data => {
            document.getElementById('local-ip').innerHTML = '<strong>Local IP:</strong> ' + data.ip;
        })
        .catch(() => {});
}

function updateConnectionStatus() {
    fetch('/status/connection')
        .then(r => r.json())
        .then(data => {
            const statusEl = document.getElementById('connection-status');
            const valueEl = document.getElementById('rtt-value');
            const timeEl = document.getElementById('timestamp');

            if (data.rtt !== null) {
                valueEl.textContent = data.rtt.toFixed(1) + ' ms';
                statusEl.className = 'status-display ' + data.status;
            } else {
                valueEl.textContent = '--';
                statusEl.className = 'status-display bad';
            }
            timeEl.textContent = 'Last updated: ' + data.timestamp;
        })
        .catch(() => {});
}

// ================= Update functions =================
function loadCurrentVersion() {
    fetch('/update/current')
        .then(r => r.json())
        .then(data => {
            document.getElementById('update-current-version').textContent = data.current || '—';
        })
        .catch(() => {});
}

function escapeHtml(s) {
    const div = document.createElement('div');
    div.textContent = s;
    return div.innerHTML;
}

// Renders "## vX.Y.Z (date)\n- item\n- item" changelog sections as
// a heading + bullet list per version, newest first.
function renderChangelogHtml(text) {
    if (!text) return '';
    return text.split(/\n\n(?=## )/).map(section => {
        const lines = section.split('\n');
        const header = lines[0].replace(/^##\s*/, '');
        const items = lines.slice(1)
            .filter(l => l.trim().startsWith('-'))
            .map(l => `<li>${escapeHtml(l.trim().slice(1).trim())}</li>`)
            .join('');
        return `<div style="margin-bottom:14px;">` +
            `<h4 style="margin:0 0 6px;color:#2d6cdf;">${escapeHtml(header)}</h4>` +
            `<ul style="margin:0;padding-left:20px;">${items}</ul>` +
            `</div>`;
    }).join('');
}

function checkForUpdate() {
    const applyBtn = document.getElementById('apply-update-btn');
    const msgEl = document.getElementById('update-status-msg');
    const changelogEl = document.getElementById('update-changelog');
    applyBtn.disabled = true;
    applyBtn.style.opacity = 0.5;
    changelogEl.style.display = 'none';
    changelogEl.innerHTML = '';
    msgEl.textContent = 'Checking for updates...';
    fetch('/update/check')
        .then(r => r.json())
        .then(data => {
            document.getElementById('update-current-version').textContent = data.current || '—';
            document.getElementById('update-latest-version').textContent = data.latest || '—';
            if (data.error) {
                msgEl.textContent = '❌ ' + data.error;
                return;
            }
            if (data.update_available) {
                msgEl.textContent = 'Update available.';
                if (data.changelog) {
                    changelogEl.innerHTML = renderChangelogHtml(data.changelog);
                    changelogEl.style.display = 'block';
                }
                applyBtn.disabled = false;
                applyBtn.style.opacity = 1;
            } else {
                msgEl.textContent = 'You are running the latest version.';
            }
        })
        .catch(() => { msgEl.textContent = '❌ Network error'; });
}

function applyUpdate() {
    if (!confirm('Update to the latest version and restart all services now?')) return;
    const applyBtn = document.getElementById('apply-update-btn');
    const msgEl = document.getElementById('update-status-msg');
    applyBtn.disabled = true;
    applyBtn.style.opacity = 0.5;
    msgEl.textContent = 'Updating...';
    fetch('/update/apply', {method: 'POST'})
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                msgEl.textContent = '🔄 ' + data.message;
                showToast('✅ Update started, panel restarting...', true);
                setTimeout(() => { location.reload(); }, 8000);
            } else {
                msgEl.textContent = '❌ ' + data.message;
                applyBtn.disabled = false;
                applyBtn.style.opacity = 1;
            }
        })
        .catch(() => {
            msgEl.textContent = '❌ Network error';
            applyBtn.disabled = false;
            applyBtn.style.opacity = 1;
        });
}

// Tab switch handler extension
document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', function() {
        const tabName = this.dataset.tab;
        if (tabName === 'main') loadRelays();
        if (tabName === 'trx') loadBandRules();
        if (tabName === 'audio') { loadAudioState(); loadConfig(); }
        if (tabName === 'config') { loadConfig(); loadLocalIp(); updateConnectionStatus(); }
        if (tabName === 'update') loadCurrentVersion();
    });
});

// Auto-refresh connection status every 2 seconds when Config tab is active
setInterval(() => {
    const activePanel = document.querySelector('.panel.active');
    if (activePanel && activePanel.id === 'config-panel') {
        updateConnectionStatus();
    }
}, 2000);

// ================= Band Relay functions =================
let bandRules = [];

function loadBandRules() {
    fetch('/bandrelay/rules')
        .then(r => r.json())
        .then(rules => {
            bandRules = rules;
            renderBandRules();
        })
        .catch(() => {});
    // Also load current state
    fetch('/bandrelay/state')
        .then(r => r.json())
        .then(data => {
            document.getElementById('bandrelay-current-freq').textContent =
                data.freq_khz ? data.freq_khz.toFixed(1) : '---';
            document.getElementById('bandrelay-current-relays').textContent =
                data.active_relays.length ? data.active_relays.map(r => r+1).join(', ') : 'none';
            document.getElementById('bandrelay-enabled').checked = data.enabled;
        })
        .catch(() => {});
}

function toggleBandRelay() {
    const enabled = document.getElementById('bandrelay-enabled').checked;
    fetch('/bandrelay/toggle', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled: enabled})
    }).then(r => {
        if (r.ok) {
            showToast(enabled ? '🔁 Auto relay switching ON' : '⏸️ Auto relay switching OFF', true);
        }
    }).catch(() => showToast('❌ Network error', false));
}

function renderBandRules() {
    const tbody = document.getElementById('bandrelay-table-body');
    tbody.innerHTML = '';
    if (!bandRules.length) {
        tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;padding:20px;color:#666;">No rules configured. Click "+ Add Rule" to create one.</td></tr>';
        return;
    }
    bandRules.forEach((rule, idx) => {
        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid #333';
        tr.innerHTML = `
            <td style="padding:6px;"><input type="number" class="br-from" value="${rule.from}" min="0" max="60000" style="width:100px;"></td>
            <td style="padding:6px;"><input type="number" class="br-to" value="${rule.to}" min="0" max="60000" style="width:100px;"></td>
            <td style="padding:6px;"><input type="text" class="br-relays" value="${rule.relays.map(r => r+1).join(',')}" placeholder="e.g. 1,2,16" style="width:100%;"></td>
            <td style="padding:6px;text-align:center;">
                <button class="btn-small btn-danger" onclick="deleteBandRule(${idx})">✕</button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function addBandRule() {
    bandRules.push({from: 7000, to: 7300, relays: []});
    renderBandRules();
    showToast('➕ New rule added. Set values and click "Save All Rules".', true);
}

function deleteBandRule(idx) {
    bandRules.splice(idx, 1);
    renderBandRules();
    showToast('🗑️ Rule removed. Click "Save All Rules" to persist.', true);
}

function saveBandRules() {
    // Read values from inputs
    const rows = document.querySelectorAll('#bandrelay-table-body tr');
    const newRules = [];
    let hasError = false;
    rows.forEach((tr, idx) => {
        const fromInput = tr.querySelector('.br-from');
        const toInput = tr.querySelector('.br-to');
        const relaysInput = tr.querySelector('.br-relays');
        if (!fromInput || !toInput || !relaysInput) return;
        const fromVal = parseInt(fromInput.value);
        const toVal = parseInt(toInput.value);
        if (isNaN(fromVal) || isNaN(toVal) || fromVal < 0 || toVal < 0 || fromVal >= toVal) {
            showToast(`❌ Rule ${idx+1}: invalid frequency range`, false);
            hasError = true;
            return;
        }
        const relays = relaysInput.value.split(',')
            .map(s => parseInt(s.trim()))
            .filter(n => !isNaN(n) && n >= 1 && n <= 16)
            .map(n => n - 1); // convert to 0-based
        newRules.push({from: fromVal, to: toVal, relays: relays});
    });
    if (hasError) return;

    fetch('/bandrelay/rules', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(newRules)
    }).then(r => {
        if (r.ok) {
            showToast('✅ Band relay rules saved!', true);
            bandRules = newRules;
            loadBandRules(); // refresh
        } else {
            return r.text().then(t => showToast('❌ ' + t, false));
        }
    }).catch(() => showToast('❌ Network error', false));
}

// Auto-refresh bandrelay state when TRX tab is active
setInterval(() => {
    const activePanel = document.querySelector('.panel.active');
    if (activePanel && activePanel.id === 'trx-panel') {
        fetch('/bandrelay/state')
            .then(r => r.json())
            .then(data => {
                document.getElementById('bandrelay-current-freq').textContent =
                    data.freq_khz ? data.freq_khz.toFixed(1) : '---';
                document.getElementById('bandrelay-current-relays').textContent =
                    data.active_relays.length ? data.active_relays.map(r => r+1).join(', ') : 'none';
            })
            .catch(() => {});
    }
}, 2000);

// Auto-refresh relay state + PTT status on Main tab every 2 seconds
setInterval(() => {
    const activePanel = document.querySelector('.panel.active');
    if (activePanel && activePanel.id === 'main-panel') {
        Promise.all([
            fetch('/state').then(r => r.json()),
            fetch('/ptt/status').then(r => r.json()),
        ])
            .then(([stateData, pttData]) => {
                relayState = stateData.state;
                relayNames = stateData.names;
                relayMode = stateData.mode;
                pttActive = pttData.active;
                document.getElementById('mode0_label').textContent = relayMode[0];
                document.getElementById('mode1_label').textContent = relayMode[1];
                renderRelays();
            })
            .catch(() => {});
    }
}, 2000);

// Initialize
loadRelays();
loadTrxConfig();
checkPttStatus();
showToast('🎉 Welcome to NanoPi Controller!', true);