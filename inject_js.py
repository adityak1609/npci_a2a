"""Inject new admin JS functions into frontend/index.html"""
import re

with open(r'd:\RAG_PDF_CHATBOT\frontend\index.html', 'r', encoding='utf-8') as f:
    content = f.read()

marker = '// Welcome HTML (DRY helper)'
if marker not in content:
    print('MARKER NOT FOUND')
    raise SystemExit(1)

new_js = r"""// KB Status helper
// ---------------------------------
function updateKbStatus(ready, label) {
    var dot = document.getElementById('admin-kb-dot');
    var lbl = document.getElementById('admin-kb-label');
    if (!dot || !lbl) return;
    dot.classList.toggle('ready', ready);
    lbl.textContent = label || (ready ? 'Knowledge base ready' : 'Knowledge base empty');
}

// Analytics time period filter
// ---------------------------------
function setAnalyticsPeriod(days, btn) {
    analyticsPeriod = days;
    document.querySelectorAll('.analytics-filter-btn').forEach(function(b) { b.classList.remove('active'); });
    btn.classList.add('active');
    loadDashboard();
}

// Analytics 2x2 Cards
// ---------------------------------
function populateAnalyticsCards(dashData) {
    var topUsers = dashData.top_users || [];
    var recentQ  = dashData.recent_queries || [];
    var recentFB = dashData.recent_feedback || [];

    var tqEl = document.getElementById('analytics-top-questions');
    if (tqEl) {
        if (!recentQ.length) {
            tqEl.innerHTML = '<div style="color:var(--text-3);font-size:12px;padding:12px 0">No queries yet.</div>';
        } else {
            tqEl.innerHTML = recentQ.slice(0, 5).map(function(q, i) {
                return '<div style="display:flex;gap:10px;align-items:flex-start;padding:7px 0;border-bottom:1px solid var(--border);font-size:12px">' +
                    '<span style="background:hsla(38,90%,54%,0.14);color:hsl(38,90%,64%);border-radius:4px;padding:2px 6px;font-weight:700;flex-shrink:0">' + (i + 1) + '</span>' +
                    '<span style="color:var(--text-2);line-height:1.4">' + escapeHtml(q.query) + '</span>' +
                    '</div>';
            }).join('');
        }
    }

    var auEl = document.getElementById('analytics-active-users');
    if (auEl) {
        if (!topUsers.length) {
            auEl.innerHTML = '<div style="color:var(--text-3);font-size:12px;padding:12px 0">No users yet.</div>';
        } else {
            auEl.innerHTML = topUsers.slice(0, 5).map(function(u, i) {
                return '<div style="display:flex;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid var(--border)">' +
                    '<div style="width:26px;height:26px;border-radius:6px;background:var(--accent-grad);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;color:#fff;flex-shrink:0">' + getInitials(u.email) + '</div>' +
                    '<div style="flex:1;min-width:0">' +
                    '<div style="font-size:12px;font-weight:600;color:var(--text-1)">' + escapeHtml(u.name || u.email) + '</div>' +
                    '<div style="font-size:10.5px;color:var(--text-3)">' + u.query_count + ' queries</div>' +
                    '</div>' +
                    '<span style="background:hsla(38,90%,54%,0.12);color:hsl(38,90%,64%);border-radius:50px;padding:2px 8px;font-size:10.5px;font-weight:600;flex-shrink:0">#' + (i + 1) + '</span>' +
                    '</div>';
            }).join('');
        }
    }

    var tdEl = document.getElementById('analytics-top-docs');
    if (tdEl) tdEl.innerHTML = '<div style="color:var(--text-3);font-size:12px;padding:12px 0">See Documents tab for indexed files.</div>';

    var fbEl = document.getElementById('analytics-feedback');
    if (fbEl) {
        if (!recentFB.length) {
            fbEl.innerHTML = '<div style="color:var(--text-3);font-size:12px;padding:12px 0">No feedback yet.</div>';
        } else {
            fbEl.innerHTML = recentFB.slice(0, 5).map(function(item) {
                var bc = item.rating === 'positive' ? 'badge-positive' : 'badge-negative';
                var bt = item.rating === 'positive' ? '\ud83d\udc4d Helpful' : '\ud83d\udc4e Not helpful';
                return '<div style="display:flex;gap:10px;align-items:flex-start;padding:7px 0;border-bottom:1px solid var(--border);font-size:12px">' +
                    '<span class="table-badge ' + bc + '" style="flex-shrink:0;font-size:10px">' + bt + '</span>' +
                    '<div style="flex:1;min-width:0">' +
                    '<div style="color:var(--text-2);font-weight:600;font-size:11.5px">' + escapeHtml(item.user_name || '') + '</div>' +
                    '<div style="color:var(--text-3);font-size:11px">' + escapeHtml((item.answer || '').slice(0, 80)) + '</div>' +
                    '</div></div>';
            }).join('');
        }
    }
}

// Audit Log
// ---------------------------------
async function loadAuditLog() {
    var container = document.getElementById('audit-log-list');
    if (!container) return;
    container.innerHTML = '<div class="admin-loading"><div class="upload-spinner"></div> Loading\u2026</div>';
    try {
        var res = await fetch(API_BASE + '/admin/audit-log?limit=100', { headers: authHeaders() });
        if (!res.ok) throw new Error('Failed to load audit log');
        var data = await res.json();
        var entries = data.entries || [];
        if (!entries.length) {
            container.innerHTML = '<div class="admin-empty"><svg viewBox="0 0 24 24"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>No audit log entries yet.</div>';
            return;
        }
        container.innerHTML = entries.map(function(e) {
            return '<div class="audit-entry">' +
                '<div class="audit-icon"><svg viewBox="0 0 24 24">' + getAuditIcon(e.action) + '</svg></div>' +
                '<div class="audit-content">' +
                '<div class="audit-action">' + formatAction(e.action) + '</div>' +
                '<div class="audit-detail">' + escapeHtml(e.target || '') + (e.detail ? ' \u2014 ' + escapeHtml(e.detail) : '') + '</div>' +
                '<div class="audit-meta">By ' + escapeHtml(e.admin_email) + '</div>' +
                '</div>' +
                '<div class="audit-time">' + formatDashboardDate(e.created_at) + '</div>' +
                '</div>';
        }).join('');
    } catch (err) {
        container.innerHTML = '<div class="admin-empty"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>' + escapeHtml(err.message) + '</div>';
        showToast('Audit log error: ' + err.message, 'error');
    }
}

function formatAction(action) {
    return (action || '').replace(/_/g, ' ').replace(/\b\w/g, function(c) { return c.toUpperCase(); });
}

function getAuditIcon(action) {
    if (action && action.indexOf('upload') !== -1)  return '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>';
    if (action && action.indexOf('delete') !== -1)  return '<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/>';
    if (action && action.indexOf('voice') !== -1)   return '<path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/>';
    return '<path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>';
}

// Voice Notes
// ---------------------------------
var mediaRecognition = null;
var isRecording = false;

function toggleRecording() {
    if (!isRecording) startRecording(); else stopRecording();
}

function startRecording() {
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { showToast('Speech recognition not supported. Type your note instead.', 'error'); return; }
    mediaRecognition = new SR();
    mediaRecognition.continuous = true;
    mediaRecognition.interimResults = true;
    mediaRecognition.lang = 'en-US';
    var textarea = document.getElementById('voice-note-content');
    var baseText = textarea ? textarea.value : '';
    mediaRecognition.onresult = function(event) {
        var interim = '', final = '';
        for (var i = event.resultIndex; i < event.results.length; i++) {
            if (event.results[i].isFinal) final += event.results[i][0].transcript;
            else interim += event.results[i][0].transcript;
        }
        if (final) baseText += final + ' ';
        if (textarea) textarea.value = baseText + interim;
    };
    mediaRecognition.onerror = function(e) { stopRecording(); if (e.error !== 'no-speech') showToast('Recording error: ' + e.error, 'error'); };
    mediaRecognition.onend = function() { if (isRecording) stopRecording(); };
    mediaRecognition.start();
    isRecording = true;
    var btn = document.getElementById('voice-mic-btn');
    var wrap = document.getElementById('voice-mic-wrap');
    var cap = document.getElementById('voice-mic-caption');
    if (btn) btn.classList.add('recording');
    if (wrap) wrap.classList.add('recording');
    if (cap) cap.textContent = '\ud83d\udd34 Recording\u2026 click to stop';
}

function stopRecording() {
    if (mediaRecognition) { mediaRecognition.stop(); mediaRecognition = null; }
    isRecording = false;
    var btn = document.getElementById('voice-mic-btn');
    var wrap = document.getElementById('voice-mic-wrap');
    var cap = document.getElementById('voice-mic-caption');
    if (btn) btn.classList.remove('recording');
    if (wrap) wrap.classList.remove('recording');
    if (cap) cap.textContent = 'Click to start recording';
}

async function saveVoiceNote() {
    var titleEl = document.getElementById('voice-note-title');
    var contentEl = document.getElementById('voice-note-content');
    var title = (titleEl ? titleEl.value : '').trim();
    var content = (contentEl ? contentEl.value : '').trim();
    if (!title) { showToast('Please enter a note title.', 'error'); return; }
    if (!content) { showToast('Please add content to the note.', 'error'); return; }
    try {
        var res = await fetch(API_BASE + '/admin/voice-notes', {
            method: 'POST', headers: authHeaders(),
            body: JSON.stringify({ title: title, content: content }),
        });
        if (!res.ok) { var err = await res.json().catch(function() { return {}; }); throw new Error(err.detail || 'Save failed'); }
        showToast('Voice note saved!', 'success');
        if (titleEl) titleEl.value = '';
        if (contentEl) contentEl.value = '';
        loadVoiceNotes();
    } catch (err) { showToast('Save failed: ' + err.message, 'error'); }
}

async function loadVoiceNotes() {
    var container = document.getElementById('voice-notes-list');
    if (!container) return;
    container.innerHTML = '<div class="admin-loading"><div class="upload-spinner"></div> Loading\u2026</div>';
    try {
        var res = await fetch(API_BASE + '/admin/voice-notes', { headers: authHeaders() });
        if (!res.ok) throw new Error('Failed to load voice notes');
        var data = await res.json();
        var notes = data.notes || [];
        if (!notes.length) {
            container.innerHTML = '<div class="admin-empty"><svg viewBox="0 0 24 24"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>No voice notes yet. Record one above.</div>';
            return;
        }
        container.innerHTML = notes.map(function(n) {
            return '<div class="voice-note-item" id="vn-' + n.id + '">' +
                '<div class="voice-note-icon"><svg viewBox="0 0 24 24"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg></div>' +
                '<div class="voice-note-body">' +
                '<div class="voice-note-title">' + escapeHtml(n.title) + '</div>' +
                '<div class="voice-note-preview">' + escapeHtml(n.content) + '</div>' +
                '<div class="voice-note-meta">Saved ' + formatDashboardDate(n.created_at) + ' \u00b7 by ' + escapeHtml(n.admin_email) + '</div>' +
                '</div>' +
                '<button class="voice-note-delete" onclick="deleteVoiceNote(' + n.id + ')" title="Delete">' +
                '<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg>' +
                '</button></div>';
        }).join('');
    } catch (err) {
        container.innerHTML = '<div class="admin-empty"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>' + escapeHtml(err.message) + '</div>';
    }
}

async function deleteVoiceNote(noteId) {
    if (!confirm('Delete this voice note permanently?')) return;
    try {
        var res = await fetch(API_BASE + '/admin/voice-notes/' + noteId, { method: 'DELETE', headers: authHeaders() });
        if (!res.ok) throw new Error('Delete failed');
        showToast('Voice note deleted.', 'success');
        var el = document.getElementById('vn-' + noteId);
        if (el) el.remove();
    } catch (err) { showToast('Delete failed: ' + err.message, 'error'); }
}

"""

# Also patch loadDashboard to call populateAnalyticsCards
analytics_patch = "        populateAnalyticsCards(data);\n\n        const busiestDay"
content_patched = content.replace(
    '        const busiestDay',
    analytics_patch,
    1
)

if content_patched == content:
    print("WARNING: analytics patch not applied (may already be applied)")
else:
    content = content_patched

# Insert new JS functions before the Welcome HTML helper comment
content = content.replace(
    marker,
    new_js + marker,
    1
)

with open(r'd:\RAG_PDF_CHATBOT\frontend\index.html', 'w', encoding='utf-8') as f:
    f.write(content)

print('SUCCESS - file written')
