/* AgnMonitor Main JavaScript */

// --- Global Utilities & Variables ---
var currentAvailableData = { tags: {}, fields: [] };
var nicknameMap = {};
var ratesCache = {}; // 실시간 속도 계산용 캐시 (tag별 저장)
window.charts = {}; // Chart.js 인스턴스 저장용

// --- Common Modal Controller (Replaces Bootstrap JS) ---
var systemModalBackdrop = null;

function closeCommonModal(modalId) {
    const modalEl = typeof modalId === 'string' ? document.getElementById(modalId) : modalId;
    if (!modalEl) return;

    modalEl.classList.remove('show');
    if (systemModalBackdrop) {
        systemModalBackdrop.classList.remove('show');
    }
    
    // Remove keydown listener when modal closes
    document.removeEventListener('keydown', _handleModalKeydown);
    
    // 애니메이션(300ms) 후 완전히 숨김
    setTimeout(() => {
        if (!modalEl.classList.contains('show')) {
            modalEl.style.display = 'none';
            if (systemModalBackdrop) {
                systemModalBackdrop.remove();
                systemModalBackdrop = null;
            }
        }
    }, 300);
}
window.closeCommonModal = closeCommonModal;

function openCommonModal(modalId) {
    const modalEl = typeof modalId === 'string' ? document.getElementById(modalId) : modalId;
    if (!modalEl) return;

    // Bootstrap 잔재 제거
    modalEl.removeAttribute('aria-hidden');
    modalEl.removeAttribute('aria-modal');
    modalEl.removeAttribute('role');
    
    modalEl.style.display = 'block';
    
    if (!systemModalBackdrop) {
        systemModalBackdrop = document.createElement('div');
        systemModalBackdrop.className = 'modal-backdrop';
        systemModalBackdrop.style.zIndex = '9998';
        document.body.appendChild(systemModalBackdrop);
    }

    // 다음 프레임에서 애니메이션 시작
    requestAnimationFrame(() => {
        modalEl.classList.add('show');
        if (systemModalBackdrop) systemModalBackdrop.classList.add('show');
        
        // Add keydown listener when modal opens
        document.addEventListener('keydown', _handleModalKeydown);
    });
}
window.openCommonModal = openCommonModal;

function closeSystemModal() { closeCommonModal('systemModal'); }
window.closeSystemModal = closeSystemModal;

function openSystemModal() { openCommonModal('systemModal'); }
window.openSystemModal = openSystemModal;

function _updateModalDOM(title, message, showCancel, showPromptInput, defaultValue) {
    const t = document.getElementById('systemModalTitle');
    const b = document.getElementById('systemModalBody');
    const pc = document.getElementById('systemModalPromptContainer');
    const cb = document.getElementById('systemModalCancelBtn');
    
    if (t) t.innerText = title;
    if (b) b.innerHTML = message;
    if (pc) pc.style.display = showPromptInput ? 'block' : 'none';
    if (cb) cb.style.display = showCancel ? 'inline-block' : 'none';
    
    if (showPromptInput) {
        const input = document.getElementById('systemModalInput');
        if (input) input.value = defaultValue || '';
    }
}

function showConfirm(title, message, callback, options = {}) {
    if (document.activeElement) document.activeElement.blur();

    _updateModalDOM(title, message, true, false);
    
    const confirmBtn = document.getElementById('systemModalConfirmBtn');
    if (confirmBtn) {
        const newConfirmBtn = confirmBtn.cloneNode(true);
        confirmBtn.parentNode.replaceChild(newConfirmBtn, confirmBtn);
        
        newConfirmBtn.onclick = () => {
            if (!options.stayOpen) window.closeSystemModal();
            if (callback) callback();
        };
    }

    window.openSystemModal();
}
window.showConfirm = showConfirm;

function showAlert(title, message, options = {}) {
    if (document.activeElement) document.activeElement.blur();

    _updateModalDOM(title, message, false, false);
    
    const confirmBtn = document.getElementById('systemModalConfirmBtn');
    if (confirmBtn) {
        const newConfirmBtn = confirmBtn.cloneNode(true);
        confirmBtn.parentNode.replaceChild(newConfirmBtn, confirmBtn);
        
        newConfirmBtn.onclick = () => {
            if (!options.stayOpen) window.closeSystemModal();
        };
    }

    window.openSystemModal();
}
window.showAlert = showAlert;

function _handleModalKeydown(e) {
    if (e.key === 'Enter') {
        const confirmBtn = document.getElementById('systemModalConfirmBtn');
        const input = document.getElementById('systemModalInput');
        
        // If it's a prompt and input is focused, let showPrompt's handler work or just click.
        // For showConfirm/showAlert, this will trigger the action.
        if (confirmBtn && confirmBtn.onclick) {
            confirmBtn.click();
            e.preventDefault();
        }
    } else if (e.key === 'Escape') {
        window.closeSystemModal();
    }
}

function showPrompt(title, message, defaultValue, callback, options = {}) {
    if (document.activeElement) document.activeElement.blur();

    _updateModalDOM(title, message, true, true, defaultValue);
    
    const input = document.getElementById('systemModalInput');
    const confirmBtn = document.getElementById('systemModalConfirmBtn');
    
    if (confirmBtn) {
        const newConfirmBtn = confirmBtn.cloneNode(true);
        confirmBtn.parentNode.replaceChild(newConfirmBtn, confirmBtn);
        
        newConfirmBtn.onclick = () => {
            const val = input ? input.value : "";
            if (!options.stayOpen) window.closeSystemModal();
            if (callback) callback(val);
        };
        
        if (input) {
            input.onkeyup = (e) => { if (e.key === 'Enter') newConfirmBtn.click(); };
        }
    }
    
    window.openSystemModal();
    if (input) {
        setTimeout(() => {
            input.focus();
            input.select();
        }, 10);
    }
}
window.showPrompt = showPrompt;

// 모든 aria 관련 속성 주입을 원천 차단
(function() {
    const originalSetAttribute = Element.prototype.setAttribute;
    Element.prototype.setAttribute = function(name, value) {
        if ((name === 'aria-hidden' || name === 'aria-modal') && (this.id === 'systemModal' || this.closest('#systemModal'))) {
            return;
        }
        originalSetAttribute.apply(this, arguments);
    };
})();

document.addEventListener('DOMContentLoaded', () => {
    const modalEl = document.getElementById('systemModal');
    if (modalEl) {
        modalEl.removeAttribute('aria-hidden');
        modalEl.removeAttribute('aria-modal');
    }
});

// 공통 필드 맵 (한글화)
var fieldMap = {
    'usage_idle': '전체 사용률 (%)',
    'usage_user': '사용자 사용률 (%)',
    'usage_system': '시스템 사용률 (%)',
    'used_percent': '사용률 (%)',
    'used': '사용량',
    'free': '여유 공간',
    'total': '전체 용량',
    'bytes_recv': '수신 속도 (B/s)',
    'bytes_sent': '송신 속도 (B/s)',
    'packets_recv': '패킷 수신 속도',
    'packets_sent': '패킷 송신 속도',
    'read_bytes': '디스크 읽기 속도',
    'write_bytes': '디스크 쓰기 속도',
    'read_time': '읽기 지연 시간',
    'write_time': '쓰기 지연 시간',
    'value': '로그 텍스트',
    'Percent_Processor_Time': 'CPU 사용률 (%)',
    'Buffer_cache_hit_ratio': '버퍼 캐시 히트율 (%)',
    'Page_life_expectancy': '페이지 기대 수명 (초)',
    'User_Connections': '사용자 연결 수',
    'Processes_blocked': '블로킹된 프로세스 수',
    'Batch_Requests/sec': '초당 배치 요청 수',
    'SQL_Compilations/sec': '초당 SQL 컴파일 수',
    'SQL_Re-Compilations/sec': '초당 SQL 재컴파일 수',
    'Number_of_Deadlocks/sec': '초당 데드락 발생 수',
    'Lock_Wait_Time_(ms)': '잠금 대기 시간 (ms)'
};

function formatValue(source, field, val) {
    if (val === null || val === undefined || val === "N/A" || val === "...") return "...";
    
    const isRate = (source === 'net' || source === 'diskio');
    const numVal = parseFloat(val);
    if (isNaN(numVal)) return "0";
    
    if (field.includes('percent') || field === 'usage_idle' || field === 'Percent_Processor_Time' || field === 'Buffer_cache_hit_ratio') {
        return numVal.toFixed(1) + '%';
    }

    if (field.includes('ms') || field.includes('Time')) {
        return numVal.toFixed(0) + ' ms';
    }

    if (field.includes('sec')) {
        return numVal.toFixed(1) + '/s';
    }
    
    if (field.includes('bytes') || field === 'used' || field === 'free' || field === 'total') {
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let size = numVal;
        let unitIndex = 0;
        while (size >= 1024 && unitIndex < units.length - 1) {
            size /= 1024;
            unitIndex++;
        }
        let res = size.toFixed(1) + ' ' + units[unitIndex];
        if (isRate) res += '/s';
        return res;
    }
    
    return numVal.toFixed(1);
}

function showRealTimeAlert(alertData) {
    const container = document.getElementById('alert-container');
    if (!container) return;

    const alertId = 'alert-' + Date.now();
    const severityBg = alertData.severity === 'critical' ? 'bg-danger' : (alertData.severity === 'warning' ? 'bg-warning text-dark' : 'bg-info text-dark');
    const closeBtnClass = (alertData.severity === 'warning' || alertData.severity === 'info') ? '' : 'btn-close-white';
    
    const toastHtml = `
        <div id="${alertId}" class="toast show ${severityBg} mb-2" role="alert">
          <div class="toast-header ${severityBg} border-bottom border-white border-opacity-25">
            <strong class="me-auto">⚠️ Alert: ${alertData.rule_name}</strong>
            <button type="button" class="btn-close ${closeBtnClass}" onclick="document.getElementById('${alertId}').remove()"></button>
          </div>
          <div class="toast-body">
            Host: ${nicknameMap[alertData.hostname] || alertData.hostname}<br>
            Value: ${alertData.value ? (typeof alertData.value === 'number' ? alertData.value.toFixed(2) : alertData.value) : 'MATCH'}
          </div>
        </div>
    `;
    container.insertAdjacentHTML('afterbegin', toastHtml);
    setTimeout(() => { document.getElementById(alertId)?.remove(); }, 10000);
}

// --- WebSocket Management ---
function initMonitoringSocket() {
    if (window.monitoringSocket) {
        window.monitoringSocket.close();
    }
    window.monitoringSocket = new WebSocket((window.location.protocol === "https:" ? "wss" : "ws") + '://' + window.location.host + '/ws/monitoring/');
    return window.monitoringSocket;
}

// --- Dashboard (index.html) Functions ---
function handleLogScroll(panelId) {
    var el = document.getElementById(`panel-${panelId}`);
    if (!el) return;
    
    if (el.scrollTop < 10 && !el.getAttribute('data-loading')) {
        var offset = el.children.length;
        el.setAttribute('data-loading', 'true');
        window.monitoringSocket.send(JSON.stringify({
            'action': 'get_more_logs',
            'panel_id': panelId,
            'offset': offset
        }));
    }
}

function appendOlderLogs(panelId, logs) {
    var el = document.getElementById(`panel-${panelId}`);
    if (!el || !logs || logs.length === 0) {
        if (el) el.removeAttribute('data-loading');
        return;
    }

    var oldScrollHeight = el.scrollHeight;
    var oldScrollTop = el.scrollTop;

    logs.forEach(line => {
        var lineEl = document.createElement('div');
        lineEl.className = 'log-line';
        lineEl.innerText = line;
        el.prepend(lineEl);
    });

    var newScrollHeight = el.scrollHeight;
    el.scrollTop = oldScrollTop + (newScrollHeight - oldScrollHeight);
    el.removeAttribute('data-loading');
}

// --- Custom GangChart Engine ---
class GangChart {
    constructor(canvas, options = {}) {
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d');
        // 40개의 실제 데이터 + 1개의 예측용 플레이스홀더 = 41개
        this.targetData = Array(41).fill(null);
        this.displayData = Array(41).fill(null);
        this.mouseX = null;
        this.lastPushTime = Date.now();
        this.lastUpdateInterval = 2000;
        this.lastTheme = null;
        
        this.updateColors();

        this.options = {
            borderColor: options.borderColor || this.colors.border,
            backgroundColor: options.backgroundColor || this.colors.fill,
            lineWidth: options.lineWidth || 2,
            isPercentage: options.isPercentage || false,
            padding: options.padding || { top: 12, bottom: 2, left: 2, right: 2 },
            ...options
        };

        this.canvas.addEventListener('mousemove', (e) => {
            const rect = this.canvas.getBoundingClientRect();
            this.mouseX = e.clientX - rect.left;
        });

        this.canvas.addEventListener('mouseleave', () => {
            this.mouseX = null;
        });

        // 부모 컨테이너 크기 변화 감지 (GridStack 배치 완료 시점 포함)
        if (window.ResizeObserver) {
            this.resizeObserver = new ResizeObserver(() => {
                this.resize();
            });
            if (this.canvas.parentElement) {
                this.resizeObserver.observe(this.canvas.parentElement);
            }
        }

        this.resize();
        this.animate();
    }

    destroy() {
        if (this.resizeObserver) {
            this.resizeObserver.disconnect();
        }
    }

    updateColors() {
        const currentTheme = document.documentElement.getAttribute('data-bs-theme') || 'dark';
        if (this.lastTheme === currentTheme) return;
        this.lastTheme = currentTheme;

        const style = getComputedStyle(document.documentElement);
        this.colors = {
            border: style.getPropertyValue('--brand-color').trim() || '#0dcaf0',
            bg: style.getPropertyValue('--log-bg').trim() || '#000000',
            fill: currentTheme === 'dark' ? 'rgba(13, 202, 240, 0.12)' : 'rgba(13, 202, 240, 0.18)'
        };
        
        if (this.options) {
            this.options.borderColor = this.colors.border;
            this.options.backgroundColor = this.colors.fill;
        }
    }

    resize() {
        const parent = this.canvas.parentElement;
        if (!parent) return;
        this.canvas.width = parent.clientWidth * window.devicePixelRatio;
        this.canvas.height = parent.clientHeight * window.devicePixelRatio;
        this.canvas.style.width = parent.clientWidth + 'px';
        this.canvas.style.height = parent.clientHeight + 'px';
        this.ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
    }

    push(val) {
        const now = Date.now();
        const diff = now - this.lastPushTime;
        if (diff > 500) {
            this.lastUpdateInterval = diff;
        }
        this.lastPushTime = now;

        this.targetData.shift();
        this.displayData.shift();

        this.targetData[39] = val;
        
        const validPoints = this.targetData.slice(0, 40).filter(v => v !== null);
        const avg = validPoints.length > 0 ? validPoints.reduce((a, b) => a + b, 0) / validPoints.length : val;
        this.targetData[40] = avg;

        if (this.displayData[39] === null) this.displayData[39] = val;
        this.displayData[40] = this.displayData[39];
    }

    setData(newData) {
        const data = Array(41).fill(null);
        const slice = newData.slice(-40);
        for (let i = 0; i < slice.length; i++) {
            data[i + (40 - slice.length)] = slice[i];
        }
        const lastVal = slice.length > 0 ? slice[slice.length - 1] : 0;
        data[40] = lastVal;

        this.targetData = [...data];
        this.displayData = [...data];
    }

    animate() {
        const now = Date.now();
        let progress = (now - this.lastPushTime) / this.lastUpdateInterval;
        if (progress > 1) progress = 1;

        for (let i = 0; i < this.targetData.length; i++) {
            if (this.targetData[i] !== null) {
                if (this.displayData[i] === null) {
                    this.displayData[i] = this.targetData[i];
                } else {
                    this.displayData[i] += (this.targetData[i] - this.displayData[i]) * 0.2;
                }
            }
        }
        
        this.render(progress);
        requestAnimationFrame(() => this.animate());
    }

    render(progress = 1) {
        this.updateColors(); // 테마 변경 감지 및 색상 갱신

        const width = this.canvas.width / window.devicePixelRatio;
        const height = this.canvas.height / window.devicePixelRatio;
        const ctx = this.ctx;
        const p = this.options.padding;

        const drawW = width - p.left - p.right;
        const drawH = height - p.top - p.bottom;

        ctx.fillStyle = this.colors.bg;
        ctx.fillRect(0, 0, width, height);

        const validTarget = this.targetData.filter(v => v !== null);
        const validDisplay = this.displayData.filter(v => v !== null);
        if (validDisplay.length === 0) return;

        let min = Math.min(...validTarget);
        let max = Math.max(...validTarget);

        if (this.options.isPercentage) {
            min = 0; max = 100;
        } else {
            const range = max - min;
            if (range === 0) { min -= 1; max += 1; }
            else { min -= range * 0.15; max += range * 0.15; }
        }

        const stepX = drawW / 40;
        const offsetX = (1 - progress) * stepX;

        const getY = (v) => {
            if (v === null) return null;
            return p.top + drawH - ((v - min) / (Math.max(0.0001, max - min))) * drawH;
        };

        ctx.strokeStyle = 'rgba(255, 255, 255, 0.05)';
        ctx.lineWidth = 1;
        for (let i = 0; i <= 4; i++) {
            const gy = p.top + (drawH / 4) * i;
            ctx.beginPath(); ctx.moveTo(p.left, gy); ctx.lineTo(p.left + drawW, gy); ctx.stroke();
        }

        let points = [];
        for (let i = 0; i < this.displayData.length; i++) {
            const y = getY(this.displayData[i]);
            if (y !== null) {
                points.push({ x: p.left + i * stepX + (offsetX - stepX), y: y, val: this.displayData[i] });
            }
        }

        if (points.length > 1) {
            ctx.save();
            ctx.beginPath();
            ctx.rect(p.left, 0, drawW, height);
            ctx.clip();

            ctx.beginPath();
            ctx.strokeStyle = this.options.borderColor;
            ctx.lineWidth = this.options.lineWidth;
            ctx.lineJoin = 'round';
            ctx.lineCap = 'round';
            ctx.moveTo(points[0].x, points[0].y);
            for (let i = 0; i < points.length - 1; i++) {
                const xc = (points[i].x + points[i + 1].x) / 2;
                const yc = (points[i].y + points[i + 1].y) / 2;
                ctx.quadraticCurveTo(points[i].x, points[i].y, xc, yc);
            }
            ctx.lineTo(points[points.length - 1].x, points[points.length - 1].y);
            ctx.stroke();

            ctx.lineTo(points[points.length - 1].x, p.top + drawH);
            ctx.lineTo(points[0].x, p.top + drawH);
            ctx.closePath();
            ctx.fillStyle = this.options.backgroundColor;
            ctx.fill();

            ctx.restore();
        }

        if (this.mouseX !== null && points.length > 0) {
            let closest = points[0];
            let minDist = 9999;
            for (let pt of points) {
                if (pt.x < p.left - 1 || pt.x > p.left + drawW + 1) continue;
                let dist = Math.abs(this.mouseX - pt.x);
                if (dist < minDist) { minDist = dist; closest = pt; }
            }
            if (minDist < 20) {
                ctx.strokeStyle = 'rgba(255, 255, 255, 0.3)';
                ctx.setLineDash([5, 5]);
                ctx.beginPath(); ctx.moveTo(closest.x, p.top); ctx.lineTo(closest.x, p.top + drawH); ctx.stroke();
                ctx.setLineDash([]);
                ctx.fillStyle = this.options.borderColor;
                ctx.beginPath(); ctx.arc(closest.x, closest.y, 4, 0, Math.PI * 2); ctx.fill();
                const txt = this.options.isPercentage ? closest.val.toFixed(1) + '%' : closest.val.toFixed(1);
                ctx.font = 'bold 11px Arial';
                const txtW = ctx.measureText(txt).width;
                ctx.fillStyle = 'rgba(0, 0, 0, 0.8)';
                ctx.fillRect(closest.x - (txtW / 2) - 5, closest.y - 25, txtW + 10, 18);
                ctx.fillStyle = '#fff'; ctx.textAlign = 'center'; ctx.fillText(txt, closest.x, closest.y - 12);
            }
        } else if (validTarget.length > 0) {
            const lastVal = validTarget[validTarget.length - 2] || validTarget[validTarget.length - 1];
            ctx.fillStyle = '#adb5bd';
            ctx.font = '9px Arial';
            ctx.textAlign = 'right';
            ctx.fillText(this.options.isPercentage ? lastVal.toFixed(1) + '%' : lastVal.toFixed(1), width - 5, 12);
        }
    }

    destroy() {}
}

function initChart(id, type, title) {
    const canvas = document.getElementById(`chart-${id}`);
    const infoEl = document.getElementById(`panel-${id}`);
    if (!canvas || !infoEl) return null;

    const field = infoEl.getAttribute('data-field') || '';
    const isPercentage = field.includes('percent') || field === 'usage_idle' || field === 'Buffer_cache_hit_ratio';

    const chart = new GangChart(canvas, {
        isPercentage: isPercentage
    });

    window.charts[id] = chart;
    return chart;
}

function updateMetrics(metrics, isRealtime) {
    for (const [id, val] of Object.entries(metrics)) {
        const el = document.getElementById(`panel-${id}`);
        if (!el) continue;

        const source = el.getAttribute('data-source');
        const chartType = el.getAttribute('data-type');

        if (source === 'custom_logs') {
            const logs = Array.isArray(val) ? val : [val];
            if (isRealtime) {
                const fragment = document.createDocumentFragment();
                logs.forEach(line => {
                    const lineEl = document.createElement('div');
                    lineEl.className = 'log-line';
                    lineEl.innerText = line;
                    fragment.appendChild(lineEl);
                });
                el.appendChild(fragment);
                
                while (el.children.length > 200) el.removeChild(el.firstChild);
                
                // Only scroll if already near bottom to avoid "stealing" scroll focus
                if (el.scrollHeight - el.scrollTop - el.clientHeight < 100) {
                    el.scrollTop = el.scrollHeight;
                }
            } else {
                el.innerHTML = logs.map(line => `<div class="log-line">${line}</div>`).join('');
                el.scrollTop = el.scrollHeight;
            }
            continue;
        }

        if (chartType === 'line') {
            let chart = window.charts[id];
            if (!chart) {
                const title = el.closest('.grid-stack-item-content')?.querySelector('.card-title-text')?.innerText || 'Chart';
                chart = initChart(id, chartType, title);
                if (!chart) continue;
            }

            if (!isRealtime && Array.isArray(val)) {
                // 초기 히스토리 (최대 40개)
                const data = Array(40).fill(null);
                const slice = val.slice(-40);
                for (let i = 0; i < slice.length; i++) {
                    data[40 - slice.length + i] = slice[i].v;
                }
                chart.setData(data);
            } else {
                // 실시간 갱신
                let finalVal = 0;
                if (['net', 'diskio', 'cpu', 'win_cpu'].includes(source) || source.startsWith('sql_')) {
                    const items = Array.isArray(val) ? val : [{v: val, t: {}}];
                    if (source === 'cpu' || source === 'win_cpu' || source.startsWith('sql_')) {
                        let totalCore;
                        if (source === 'cpu') {
                            totalCore = items.find(item => item.t.cpu === 'cpu-total');
                        } else if (source === 'win_cpu') {
                            totalCore = items.find(item => item.t.instance === '_Total');
                        } else if (source.startsWith('sql_')) {
                            // SQL Server metrics: prefer _Total if exists, else first item
                            totalCore = items.find(item => item.t.instance === '_Total') || items[0];
                        }
                        finalVal = totalCore ? totalCore.v : (items.reduce((acc, curr) => acc + curr.v, 0) / (items.length || 1));
                    } else {
                        const now = Date.now();
                        items.forEach(item => {
                            const tagKey = JSON.stringify(item.t);
                            const cacheKey = `${id}-${tagKey}`;
                            const prev = ratesCache[cacheKey];
                            if (prev) {
                                const tDiff = (now - prev.time) / 1000;
                                if (tDiff > 1.5 && tDiff < 30) {
                                    const vDiff = item.v - prev.val;
                                    if (vDiff >= 0) finalVal += vDiff / tDiff;
                                }
                            }
                            ratesCache[cacheKey] = { val: item.v, time: now };
                        });
                    }
                } else {
                    finalVal = parseFloat(val);
                }
                
                if (isNaN(finalVal)) finalVal = 0;
                chart.push(finalVal);
            }
        } else if (isRealtime) {
            // 텍스트 패널 업데이트 (생략 가능하지만 기존 로직 유지)
            let displayVal = 0;
            if (['net', 'diskio', 'cpu', 'win_cpu'].includes(source) || source.startsWith('sql_')) {
                const items = Array.isArray(val) ? val : [{v: val, t: {}}];
                if (source === 'cpu' || source === 'win_cpu' || source.startsWith('sql_')) {
                    let totalCore;
                    if (source === 'cpu') {
                        totalCore = items.find(item => item.t.cpu === 'cpu-total');
                    } else if (source === 'win_cpu') {
                        totalCore = items.find(item => item.t.instance === '_Total');
                    } else if (source.startsWith('sql_')) {
                        totalCore = items.find(item => item.t.instance === '_Total') || items[0];
                    }
                    displayVal = totalCore ? totalCore.v : (items.reduce((acc, curr) => acc + curr.v, 0) / (items.length || 1));
                } else {
                    const now = Date.now();
                    items.forEach(item => {
                        const tagKey = JSON.stringify(item.t);
                        const cacheKey = `${id}-${tagKey}`;
                        const prev = ratesCache[cacheKey];
                        if (prev) {
                            const tDiff = (now - prev.time) / 1000;
                            if (tDiff > 1.5 && tDiff < 30) {
                                const vDiff = item.v - prev.val;
                                if (vDiff >= 0) displayVal += vDiff / tDiff;
                            }
                        }
                        ratesCache[cacheKey] = { val: item.v, time: now };
                    });
                }
            } else {
                displayVal = parseFloat(val);
            }
            const field = el.getAttribute('data-field');
            el.innerText = formatValue(source, field, displayVal);
            adjustMetricFontSize(el);
        }
    }
}

function adjustMetricFontSize(el) {
    el.style.fontSize = '1.8rem';
    const parent = el.parentElement;
    if (!parent || parent.clientWidth === 0) return;
    const ratio = parent.clientWidth / el.scrollWidth;
    if (ratio < 1) {
        el.style.fontSize = Math.max(0.7, 1.8 * ratio * 0.9) + 'rem';
    }
}

// --- Alert Functions ---
function renderAlertHistory(history) {
    const tbody = document.getElementById('alert-history-list');
    if (!tbody) return;
    tbody.innerHTML = '';
    history.forEach(h => {
        const severityColor = h.severity === 'critical' ? 'text-danger' : (h.severity === 'warning' ? 'text-warning' : 'text-info');
        const tr = document.createElement('tr');
        const originDisplay = nicknameMap[h.origin] || h.origin || '-';
        const valueDisplay = (typeof h.value === 'number') ? h.value.toFixed(2) : (h.value ? h.value : 'MATCH');
        tr.innerHTML = `
            <td><div class="fw-bold">${h.rule_name}</div></td>
            <td class="small text-muted">${originDisplay}</td>
            <td class="${severityColor} fw-bold">${valueDisplay}</td>
            <td class="small text-muted">${h.timestamp}</td>
            <td class="text-end"><button class="btn btn-sm btn-outline-success" onclick="resolveAlert(${h.id})">OK</button></td>
        `;
        tbody.appendChild(tr);
    });
}

function renderAlertArchive(history) {
    const tbody = document.getElementById('alert-archive-list');
    if (!tbody) return;
    tbody.innerHTML = '';
    history.forEach(h => {
        const severityColor = h.severity === 'critical' ? 'text-danger' : (h.severity === 'warning' ? 'text-warning' : 'text-info');
        const tr = document.createElement('tr');
        const originDisplay = nicknameMap[h.origin] || h.origin || '-';
        const valueDisplay = (typeof h.value === 'number') ? h.value.toFixed(2) : (h.value ? h.value : 'MATCH');
        tr.innerHTML = `
            <td><div class="fw-bold">${h.rule_name}</div></td>
            <td class="small text-muted">${originDisplay}</td>
            <td class="${severityColor} fw-bold">${valueDisplay}</td>
            <td class="small text-muted">${h.timestamp}</td>
            <td class="text-end text-success fw-bold small">RESOLVED</td>
        `;
        tbody.appendChild(tr);
    });
}

function resolveAlert(id) {
    socket.send(JSON.stringify({
        'action': 'resolve_alert',
        'id': id
    }));
}
