// Application State
let activeTab = 'dashboard';
let configData = { subscriptions: [], filters: { exclude: [], include: [] } };
let systemStatus = {};
let activeSessions = [];
let groupedNodes = {};
let loadedSubIds = new Set();
let logBuffer = [];
let lastFetchedLogCount = 0;
let isSyncing = false;
let selectedCountryTab = 'ALL';
let nodeLatencies = {}; // name -> delay ms
let authInfo = { auth_required: false, two_factor_enabled: false };

// DOM Elements
const navItems = document.querySelectorAll('.nav-item');
const tabPanes = document.querySelectorAll('.tab-pane');
const currentTabTitle = document.getElementById('currentTabTitle');
const currentTabSub = document.getElementById('currentTabSub');

const statMihomo = document.getElementById('statMihomo');
const statConnections = document.getElementById('statConnections');
const statSessions = document.getElementById('statSessions');
const statNodes = document.getElementById('statNodes');
const corePulse = document.getElementById('corePulse');
const coreStatusText = document.getElementById('coreStatusText');

const portsTableBody = document.getElementById('portsTableBody');
const badgeCountryCount = document.getElementById('badgeCountryCount');
const subList = document.getElementById('subList');
const excludeFilters = document.getElementById('excludeFilters');
const includeFilters = document.getElementById('includeFilters');

const btnSync = document.getElementById('btnSync');
const syncBtnText = document.getElementById('syncBtnText');
const btnSaveFilters = document.getElementById('btnSaveFilters');

const countryTabs = document.getElementById('countryTabs');
const selectedCountryName = document.getElementById('selectedCountryName');
const selectedCountryCount = document.getElementById('selectedCountryCount');
const nodesGrid = document.getElementById('nodesGrid');
const btnPingAll = document.getElementById('btnPingAll');

const sessionsTableBody = document.getElementById('sessionsTableBody');
const sessionCountBadge = document.getElementById('sessionCountBadge');

const terminal = document.getElementById('terminal');
const chkAutoScroll = document.getElementById('chkAutoScroll');
const btnClearLogs = document.getElementById('btnClearLogs');

const neonAlertBanner = document.getElementById('neonAlertBanner');
const neonAlertMessage = document.getElementById('neonAlertMessage');

// Settings Elements
const settingSmartPort = document.getElementById('settingSmartPort');
const settingPortPoolStart = document.getElementById('settingPortPoolStart');
const settingSocksEnabled = document.getElementById('settingSocksEnabled');
const settingSocksUsername = document.getElementById('settingSocksUsername');
const settingSocksPassword = document.getElementById('settingSocksPassword');
const settingSocksCredentialsGroup = document.getElementById('settingSocksCredentialsGroup');
const socksCredentialsWrapper = document.getElementById('socksCredentialsWrapper');
const portCredentialsCard = document.getElementById('portCredentialsCard');
const settingStickyTTL = document.getElementById('settingStickyTTL');
const settingAlarmPercent = document.getElementById('settingAlarmPercent');
const btnSaveSystemSettings = document.getElementById('btnSaveSystemSettings');

// Modal Elements
const subModal = document.getElementById('subModal');
const btnAddSubModal = document.getElementById('btnAddSubModal');
const btnModalClose = document.getElementById('btnModalClose');
const btnModalCancel = document.getElementById('btnModalCancel');
const btnModalSave = document.getElementById('btnModalSave');
const subNameInput = document.getElementById('subName');
const subUrlInput = document.getElementById('subUrl');
const subEnabledInput = document.getElementById('subEnabled');
let editingSubIndex = -1; // -1 means adding

// Tab Metadata
const tabMetadata = {
    dashboard: { title: '大盘概览', sub: '监控整个动态代理池系统的即时运转状况' },
    subscriptions: { title: '订阅管理', sub: '配置与更新您的机场节点订阅链接及白名单/黑名单过滤规则' },
    nodes: { title: '节点视窗', sub: '实时浏览并按国家/地区分组检索您的全部可用代理节点' },
    sessions: { title: '活动会话', sub: '分析追踪当下活动的 Socks5 智能粘性会话与调度明细' },
    logs: { title: '实时日志', sub: '流式加载展现 Python 控制层与 Mihomo (Clash Meta) 后台内核的输出日志' },
    settings: { title: '系统设置', sub: '配置与更新系统的出入站、连接凭据安全及多级警报触发阈值' }
};

// ==================== NAV NAVIGATION ====================
navItems.forEach(item => {
    item.addEventListener('click', (e) => {
        e.preventDefault();
        const tab = item.getAttribute('data-tab');
        switchTab(tab);
    });
});

function switchTab(tab) {
    activeTab = tab;
    
    // Update Sidebar Active state
    navItems.forEach(i => i.classList.remove('active'));
    document.querySelector(`.nav-item[data-tab="${tab}"]`).classList.add('active');
    
    // Update Panes
    tabPanes.forEach(pane => pane.classList.remove('active'));
    document.getElementById(`pane-${tab}`).classList.add('active');
    
    // Update Title
    const meta = tabMetadata[tab] || { title: 'ProxyHub', sub: '' };
    currentTabTitle.textContent = meta.title;
    currentTabSub.textContent = meta.sub;

    // Trigger explicit refreshes on tab load
    if (tab === 'subscriptions') {
        renderSubscriptions();
    } else if (tab === 'nodes') {
        loadNodesData();
    } else if (tab === 'sessions') {
        updateSessionsTable();
    } else if (tab === 'settings') {
        loadSettingsForm();
    }
}

// ==================== API ACTIONS ====================

async function fetchAPI(endpoint, options = {}) {
    const token = localStorage.getItem('proxyhub_token');
    if (token) {
        options.headers = options.headers || {};
        options.headers['X-Access-Token'] = token;
    }

    try {
        const response = await fetch(`/api/${endpoint}`, options);
        if (response.status === 401) {
            localStorage.removeItem('proxyhub_token');
            if (typeof showLoginScreen === 'function') {
                showLoginScreen('身份认证失效或无权访问，请先登录！');
            }
            throw new Error('Unauthorized Access');
        }
        if (!response.ok) {
            throw new Error(`HTTP Error: ${response.status}`);
        }
        return await response.json();
    } catch (err) {
        console.error(`API Error on /api/${endpoint}:`, err);
        return null;
    }
}

// Poll general status and logs
async function pollStatus() {
    const data = await fetchAPI('status');
    if (data) {
        systemStatus = data;
        updateStatusDOM();
    }
    
    if (activeTab === 'logs') {
        await fetchLogs();
    }
    if (activeTab === 'sessions' || activeTab === 'dashboard') {
        await fetchSessions();
    }
}

function updateStatusDOM() {
    // 1. Update Core status
    if (systemStatus.mihomo_running) {
        statMihomo.textContent = 'RUNNING';
        statMihomo.style.color = '#10b981';
        corePulse.className = 'status-pulse running';
        coreStatusText.textContent = '内核正常运行中';
    } else {
        statMihomo.textContent = 'OFFLINE';
        statMihomo.style.color = '#f43f5e';
        corePulse.className = 'status-pulse';
        coreStatusText.textContent = '内核已离线/未启动';
    }
    
    // 2. Update Stats Counter
    statConnections.textContent = systemStatus.smart_proxy_connections || 0;
    statSessions.textContent = systemStatus.active_sessions_count || 0;
    statNodes.textContent = systemStatus.total_nodes || 0;
    
    // Refresh ports table real-time traffic statistics
    renderPortsTable();
    
    // 3. Update Sync Button state
    if (systemStatus.is_syncing) {
        isSyncing = true;
        btnSync.disabled = true;
        syncBtnText.textContent = '配置同步中...';
        btnSync.querySelector('.icon').style.animation = 'spin 1.5s linear infinite';
    } else {
        if (isSyncing) {
            // just finished syncing
            isSyncing = false;
            loadNodesData(); // reload nodes
            
            // Present detailed feedback alert dialog
            const results = systemStatus.last_sync_results || [];
            if (results.length > 0) {
                const successes = results.filter(r => r.status === 'success');
                const failures = results.filter(r => r.status === 'failure');
                
                let feedbackMsg = `🔄 【同步机场配置反馈】\n\n`;
                feedbackMsg += `✅ 成功同步: ${successes.length} 个订阅\n`;
                successes.forEach(s => {
                    feedbackMsg += `   • ${s.name}: 成功加载 ${s.count} 个节点\n`;
                });
                
                if (failures.length > 0) {
                    feedbackMsg += `\n❌ 同步失败: ${failures.length} 个订阅\n`;
                    failures.forEach(f => {
                        feedbackMsg += `   • ${f.name}: 失败原因: ${f.error}\n`;
                    });
                } else {
                    feedbackMsg += `\n✨ 所有启用订阅均同步成功！`;
                }
                
                alert(feedbackMsg);
            }
        }
        btnSync.disabled = false;
        syncBtnText.textContent = '同步机场配置';
        btnSync.querySelector('.icon').style.animation = 'none';
    }

    // 4. Update Neon Alert Banner based on system health status
    if (neonAlertBanner && neonAlertMessage) {
        if (systemStatus.system_health === 'warning') {
            neonAlertBanner.style.display = 'flex';
            neonAlertBanner.className = 'neon-alert-banner warning-level';
            neonAlertMessage.textContent = `⚠️ 节点警报: 机场订阅链接同步失败，但当前本地代理池存活可用（存活率 ${systemStatus.working_percent}% >= 限制阈值 ${systemStatus.alarm_threshold}%）`;
        } else if (systemStatus.system_health === 'alarm') {
            neonAlertBanner.style.display = 'flex';
            neonAlertBanner.className = 'neon-alert-banner';
            if (!systemStatus.mihomo_running) {
                neonAlertMessage.textContent = '🚨 严重故障: Clash 代理内核已崩溃或关闭！中转代理功能已停用，请前往日志排查！';
            } else {
                neonAlertMessage.textContent = `🚨 级联警报: 机场订阅同步失败，且可用代理节点数跌破安全底线（存活率 ${systemStatus.working_percent}% < 警报阈值 ${systemStatus.alarm_threshold}%）！`;
            }
        } else {
            neonAlertBanner.style.display = 'none';
        }
    }
}

// Fetch system config.json
async function loadConfig() {
    const data = await fetchAPI('config');
    if (data) {
        configData = data;
        
        // Populate filters
        excludeFilters.value = (configData.filters.exclude || []).join(', ');
        includeFilters.value = (configData.filters.include || []).join(', ');
        
        renderSubscriptions();
    }
}

// Save config.json back
async function saveConfig() {
    await fetchAPI('config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(configData)
    });
}

// Fetch grouped nodes
async function loadNodesData() {
    const data = await fetchAPI('nodes');
    if (data) {
        groupedNodes = data;
        renderPortsTable();
        renderNodesTabContent();
        populateGeneratorCountries();
    }
}

// Fetch active SOCKS5 sessions
async function fetchSessions() {
    const data = await fetchAPI('sessions');
    if (data) {
        activeSessions = data;
        updateSessionsTable();
    }
}

// Fetch streamed logs
async function fetchLogs() {
    const lines = await fetchAPI('logs');
    if (lines && lines.length > 0) {
        lines.forEach(line => {
            const div = document.createElement('div');
            div.className = 'terminal-line';
            
            if (line.includes('[System]')) {
                div.className += ' log-sys';
            } else if (line.includes('[Mihomo]')) {
                div.className += ' log-mihomo';
            } else if (line.includes('[SmartProxy]')) {
                div.className += ' log-smart';
            } else if (line.includes('[ERROR]') || line.includes('[warning]')) {
                div.className += ' log-err';
            }
            
            div.textContent = line;
            terminal.appendChild(div);
        });
        
        // Cap DOM elements to prevent memory leak
        const MAX_LOG_LINES = 500;
        while (terminal.children.length > MAX_LOG_LINES) {
            terminal.removeChild(terminal.firstChild);
        }
        
        if (chkAutoScroll.checked) {
            terminal.scrollTop = terminal.scrollHeight;
        }
    }
}

// ==================== RENDERING LOGIC ====================

// Render ports summary table on Dashboard
function formatBytes(bytes) {
    if (!bytes || bytes <= 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function renderPortsTable() {
    const countries = Object.keys(groupedNodes).filter(c => c !== 'GLOBAL');
    badgeCountryCount.textContent = `${countries.length} 个地区`;
    
    portsTableBody.innerHTML = '';
    
    const trafficData = systemStatus.group_traffic || {};
    const authEnabled = configData && configData.socks5_auth && configData.socks5_auth.enabled;
    const portCreds = (configData && configData.port_credentials) || {};
    
    // Update card description dynamically based on auth status
    const cardDesc = document.querySelector('.panel-right .card-desc');
    if (cardDesc) {
        if (authEnabled) {
            cardDesc.innerHTML = `<span style="color: #10b981; font-weight: 600;">🔒 安全凭证认证已启用</span>：所有路由均在 1080 端口，使用以下账号名作为代理用户名。密码为您的统一代理密码。`;
        } else {
            cardDesc.innerHTML = `所有路由均在 1080 端口，请将以下账号名填入代理用户名（无需密码）。`;
        }
    }
    
    // 1. Add Global Rows
    const globalData = groupedNodes['GLOBAL'];
    if (globalData) {
        const tr = document.createElement('tr');
        const gTraffic = trafficData['GLOBAL'] || { rx: 0, tx: 0 };
        const trafficText = `↑ ${formatBytes(gTraffic.tx)} / ↓ ${formatBytes(gTraffic.rx)}`;
        
        const gHasOverride = portCreds['GLOBAL'] && (portCreds['GLOBAL'].username || portCreds['GLOBAL'].password);
        const gLockBadge = authEnabled 
            ? (gHasOverride 
                ? '<span style="color: #10b981; margin-right: 4px; font-weight: 600;" title="使用独立安全凭证保护">🔒 [独立]</span>' 
                : '<span style="color: #10b981; margin-right: 4px;" title="安全凭证保护中">🔒</span>') 
            : '';
            
        tr.innerHTML = `
            <td>
                <div class="country-flag-cell">
                    <span>◈</span>
                    <span>GLOBAL 全局</span>
                    <span class="country-code-badge">GLOBAL</span>
                </div>
            </td>
            <td>${systemStatus.total_nodes || 0}</td>
            <td><code class="cr-code">${gLockBadge}GLOBAL-rotate</code></td>
            <td><code class="cr-code">${gLockBadge}GLOBAL-sticky</code></td>
            <td><code class="cr-code" style="color: #60a5fa; font-weight: 500;">${trafficText}</code></td>
        `;
        portsTableBody.appendChild(tr);
    }
    
    // 2. Add Country Rows
    countries.forEach(country => {
        const cData = groupedNodes[country];
        const emoji = getCountryEmoji(country);
        const tr = document.createElement('tr');
        const cTraffic = trafficData[country] || { rx: 0, tx: 0 };
        const trafficText = `↑ ${formatBytes(cTraffic.tx)} / ↓ ${formatBytes(cTraffic.rx)}`;
        
        const hasOverride = portCreds[country] && (portCreds[country].username || portCreds[country].password);
        const cLockBadge = authEnabled 
            ? (hasOverride 
                ? '<span style="color: #10b981; margin-right: 4px; font-weight: 600;" title="使用独立安全凭证保护">🔒 [独立]</span>' 
                : '<span style="color: #10b981; margin-right: 4px;" title="安全凭证保护中">🔒</span>') 
            : '';
            
        tr.innerHTML = `
            <td>
                <div class="country-flag-cell">
                    <span>${emoji}</span>
                    <span>${getCountryName(country)}</span>
                    <span class="country-code-badge">${country}</span>
                </div>
            </td>
            <td>${cData.nodes.length}</td>
            <td><code class="cr-code">${cLockBadge}${country}-rotate</code></td>
            <td><code class="cr-code">${cLockBadge}${country}-sticky</code></td>
            <td><code class="cr-code" style="color: #60a5fa; font-weight: 500;">${trafficText}</code></td>
        `;
        portsTableBody.appendChild(tr);
    });

    if (countries.length === 0 && !globalData) {
        portsTableBody.innerHTML = `<tr><td colspan="5" class="text-center py-4 text-gray">暂无已启用的机场订阅，请在“订阅管理”页配置。</td></tr>`;
    }
}

// Render subscriptions list in tab
function renderSubscriptions() {
    subList.innerHTML = '';
    
    if (configData.subscriptions.length === 0) {
        subList.innerHTML = `<div class="text-center py-4 text-gray">暂无添加机场订阅，请点击右上角按钮添加。</div>`;
        return;
    }
    
    configData.subscriptions.forEach((sub, idx) => {
        const div = document.createElement('div');
        div.className = 'sub-item';
        
        // Translate period
        const periodTextMap = {
            'month': '月付周期',
            'quarter': '季付周期',
            'year': '年付周期',
            'permanent': '永不过期'
        };
        const periodText = periodTextMap[sub.expire_period || 'permanent'] || '永不过期';
        const expireDateText = sub.expire_date ? (sub.expire_date === 'never' ? '永不过期' : sub.expire_date) : '永不过期';
        const trafficText = (sub.total_traffic_gb && sub.total_traffic_gb > 0) ? `${sub.total_traffic_gb} GB` : '无限制';
        const resetText = (sub.reset_day && sub.reset_day > 0) ? `每月 ${sub.reset_day} 号重置` : '不重置';

        const isTextSub = sub.type === 'text' || sub.url.includes('\n') || sub.url.startsWith('trojan://') || sub.url.startsWith('ss://');
        const displayUrl = isTextSub ? `📝 本地节点导入 (${sub.url.split('\n').filter(s => s.trim().length > 0).length} 个节点)` : sub.url;
        
        div.innerHTML = `
            <div class="sub-item-info">
                <h4>${sub.name} <span class="badge ${sub.enabled ? 'badge-success' : 'badge-normal'}">${sub.enabled ? '已启用' : '已禁用'}</span></h4>
                <p class="sub-url">${displayUrl}</p>
                <div class="sub-meta-tags mt-2" style="font-size: 0.85rem; color: var(--text-secondary); display: flex; flex-wrap: wrap; gap: 12px; margin-top: 6px;">
                    <span>有效周期: <strong style="color: hsl(var(--accent-indigo))">${periodText}</strong></span>
                    <span>过期日期: <strong style="color: hsl(var(--accent-emerald))">${expireDateText}</strong></span>
                    <span>总流量: <strong style="color: hsl(var(--accent-rose))">${trafficText}</strong></span>
                    <span>重置日: <strong style="color: #d97706">${resetText}</strong></span>
                </div>
            </div>
            <div class="sub-item-actions">
                <button class="btn btn-secondary btn-icon-only btn-edit-sub" data-idx="${idx}">
                    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 1 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>
                </button>
                <button class="btn btn-secondary btn-icon-only text-danger btn-delete-sub" data-idx="${idx}">
                    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                </button>
            </div>
        `;
        subList.appendChild(div);
    });

    // Wire edit and delete buttons
    document.querySelectorAll('.btn-edit-sub').forEach(btn => {
        btn.addEventListener('click', () => {
            const idx = parseInt(btn.getAttribute('data-idx'));
            openSubModal(idx);
        });
    });
    
    document.querySelectorAll('.btn-delete-sub').forEach(btn => {
        btn.addEventListener('click', () => {
            const idx = parseInt(btn.getAttribute('data-idx'));
            if (confirm(`确认要删除订阅 “${configData.subscriptions[idx].name}” 吗？`)) {
                configData.subscriptions.splice(idx, 1);
                saveConfig();
                renderSubscriptions();
            }
        });
    });
}

// Render tabs and grids inside Node Explorer
function renderNodesTabContent() {
    // Collect all countries
    const countries = Object.keys(groupedNodes);
    if (countries.length === 0) {
        countryTabs.innerHTML = '';
        nodesGrid.innerHTML = `<div class="text-center py-4 text-gray">暂无可用代理节点。请检查并更新订阅。</div>`;
        selectedCountryCount.textContent = '0 个节点';
        return;
    }

    // Build Tabs
    countryTabs.innerHTML = '';
    
    // 1. ALL Tab
    const allBtn = document.createElement('button');
    allBtn.className = `tab-btn ${selectedCountryTab === 'ALL' ? 'active' : ''}`;
    allBtn.textContent = `全部节点 (${systemStatus.total_nodes || 0})`;
    allBtn.addEventListener('click', () => {
        selectedCountryTab = 'ALL';
        renderNodesTabContent();
    });
    countryTabs.appendChild(allBtn);

    // 2. Specific Country Tabs
    countries.forEach(country => {
        const info = groupedNodes[country];
        const emoji = getCountryEmoji(country);
        const name = country === 'GLOBAL' ? '全局' : getCountryName(country);
        
        const btn = document.createElement('button');
        btn.className = `tab-btn ${selectedCountryTab === country ? 'active' : ''}`;
        btn.textContent = `${emoji} ${name} (${info.nodes.length})`;
        btn.addEventListener('click', () => {
            selectedCountryTab = country;
            renderNodesTabContent();
        });
        countryTabs.appendChild(btn);
    });

    // Build Nodes Card Grid based on selection
    nodesGrid.innerHTML = '';
    let targetNodes = [];
    
    if (selectedCountryTab === 'ALL') {
        selectedCountryName.textContent = '全部节点';
        selectedCountryCount.textContent = `${systemStatus.total_nodes || 0} 个节点`;
        
        // Flatten all nodes
        countries.forEach(c => {
            targetNodes = targetNodes.concat(groupedNodes[c].nodes);
        });
    } else {
        const countryInfo = groupedNodes[selectedCountryTab] || { nodes: [] };
        selectedCountryName.textContent = selectedCountryTab === 'GLOBAL' ? '全局混合节点' : getCountryName(selectedCountryTab);
        selectedCountryCount.textContent = `${countryInfo.nodes.length} 个节点`;
        targetNodes = countryInfo.nodes;
    }

    targetNodes.forEach(node => {
        const div = document.createElement('div');
        div.className = `node-card ${node.enabled === false ? 'disabled' : ''}`;
        
        const allCountriesList = ['HK', 'US', 'JP', 'SG', 'TW', 'KR', 'UK', 'DE', 'GLOBAL', 'Others'];
        let optionsHTML = '';
        allCountriesList.forEach(c => {
            const isSelected = node.country === c || (c === 'Others' && !node.country);
            optionsHTML += `<option value="${c}" ${isSelected ? 'selected' : ''}>${getCountryEmoji(c)} ${getCountryName(c)}</option>`;
        });

        let delayHTML = '';
        if (node.name in nodeLatencies) {
            const delay = nodeLatencies[node.name];
            if (delay > 0) {
                let latencyClass = 'latency-good';
                if (delay >= 350) latencyClass = 'latency-bad';
                else if (delay >= 150) latencyClass = 'latency-medium';
                delayHTML = `<span class="node-latency-tag ${latencyClass}" style="cursor:pointer" title="点击测速">${delay}ms</span>`;
            } else {
                delayHTML = `<span class="node-latency-tag latency-bad" style="cursor:pointer" title="点击测速">超时</span>`;
            }
        } else {
            delayHTML = `<span class="node-latency-tag latency-timeout" style="cursor:pointer; color:hsl(var(--accent-indigo))" title="点击测速">测速</span>`;
        }

        div.innerHTML = `
            <div class="node-card-top">
                <div class="node-name-wrapper">
                    <h4 title="${node.name}">${node.name}</h4>
                    <span>${node.server}:${node.port}</span>
                </div>
                <div style="display: flex; flex-direction: column; align-items: flex-end; gap: 6px;">
                    <span class="node-protocol-tag">${node.type}</span>
                    ${delayHTML}
                </div>
            </div>
            <div class="node-card-controls">
                <select class="node-country-select" data-name="${node.name}">
                    ${optionsHTML}
                </select>
                <label class="node-switch-container">
                    <input type="checkbox" class="node-toggle-input" data-name="${node.name}" ${node.enabled !== false ? 'checked' : ''}>
                    <span class="node-switch-slider"></span>
                </label>
            </div>
        `;
        
        const select = div.querySelector('.node-country-select');
        const toggle = div.querySelector('.node-toggle-input');
        const latencyTag = div.querySelector('.node-latency-tag');

        latencyTag.addEventListener('click', async (e) => {
            e.stopPropagation();
            latencyTag.textContent = '...';
            const res = await fetchAPI('nodes/ping', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ node_name: node.name })
            });
            if (res && res.status === 'success') {
                const delay = res.delays[node.name];
                nodeLatencies[node.name] = delay;
                renderNodesTabContent();
            } else {
                latencyTag.textContent = '超时';
            }
        });
        
        select.addEventListener('change', async () => {
            div.classList.add('node-card-saving');
            const res = await fetchAPI('node/override', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    node_name: node.name,
                    country: select.value
                })
            });
            div.classList.remove('node-card-saving');
            if (res && res.status === 'success') {
                node.country = select.value;
                setTimeout(loadNodesData, 1000);
            } else {
                alert('修改地区失败，请检查网络！');
            }
        });
        
        toggle.addEventListener('change', async () => {
            div.classList.add('node-card-saving');
            const res = await fetchAPI('node/override', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    node_name: node.name,
                    enabled: toggle.checked
                })
            });
            div.classList.remove('node-card-saving');
            if (res && res.status === 'success') {
                node.enabled = toggle.checked;
                if (!toggle.checked) {
                    div.classList.add('disabled');
                } else {
                    div.classList.remove('disabled');
                }
                setTimeout(loadNodesData, 1000);
            } else {
                alert('更改节点状态失败！');
                toggle.checked = !toggle.checked;
            }
        });
        
        nodesGrid.appendChild(div);
    });
}

// Update Active Sticky Sessions
function updateSessionsTable() {
    sessionCountBadge.textContent = `${activeSessions.length} 个活动会话`;
    sessionsTableBody.innerHTML = '';
    
    if (activeSessions.length === 0) {
        sessionsTableBody.innerHTML = `<tr><td colspan="4" class="text-center py-4 text-gray">暂无活动中的粘性会话连接</td></tr>`;
        return;
    }
    
    activeSessions.forEach(session => {
        // Parse session key format: COUNTRY-strategy[-sessionId]
        // e.g. "US-sticky-sess_abc123", "GLOBAL-rotate", "HK-sticky-my-session"
        const match = session.match(/^([A-Z]+)-(rotate|sticky)(?:-(.+))?$/);
        let country, strategy, sessId;
        if (match) {
            country = match[1];
            strategy = match[2];
            sessId = match[3] || '默认';
        } else {
            country = 'GLOBAL';
            strategy = 'rotate';
            sessId = session || '未指定';
        }
        
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><code class="cr-code">${sessId}</code></td>
            <td>
                <div class="country-flag-cell">
                    <span>${getCountryEmoji(country)}</span>
                    <span>${getCountryName(country)}</span>
                </div>
            </td>
            <td><span class="badge badge-accent">${strategy.toUpperCase()}</span></td>
            <td><span class="badge badge-success">ACTIVE</span></td>
        `;
        sessionsTableBody.appendChild(tr);
    });
}

// ==================== EVENT HANDLERS ====================

// Manual Sync Subscriptions
btnSync.addEventListener('click', async () => {
    btnSync.disabled = true;
    syncBtnText.textContent = '正在发起同步...';
    
    const res = await fetchAPI('sync', { method: 'POST' });
    if (res && res.status === 'success') {
        isSyncing = true;
        pollStatus();
    } else {
        alert('发起同步失败：' + (res ? res.message : '未知网络错误'));
        btnSync.disabled = false;
        syncBtnText.textContent = '同步机场配置';
    }
});

// Save filters configs
btnSaveFilters.addEventListener('click', async () => {
    btnSaveFilters.disabled = true;
    btnSaveFilters.textContent = '正在保存...';
    
    // Parse filters string into list
    const excludes = excludeFilters.value.split(',').map(s => s.trim()).filter(s => s.length > 0);
    const includes = includeFilters.value.split(',').map(s => s.trim()).filter(s => s.length > 0);
    
    configData.filters.exclude = excludes;
    configData.filters.include = includes;
    
    await saveConfig();
    
    alert('清洗过滤规则保存成功！重新同步后生效。');
    btnSaveFilters.disabled = false;
    btnSaveFilters.textContent = '保存过滤设置';
});

// Modal Operations
btnAddSubModal.addEventListener('click', () => openSubModal());
btnModalClose.addEventListener('click', closeSubModal);
btnModalCancel.addEventListener('click', closeSubModal);

function openSubModal(idx = -1) {
    editingSubIndex = idx;
    
    const periodSelect = document.getElementById('subExpirePeriod');
    const dateInput = document.getElementById('subExpireDate');
    const dateGroup = document.getElementById('subExpireDateGroup');
    const trafficInput = document.getElementById('subTotalTraffic');
    const resetInput = document.getElementById('subResetDay');
    
    const typeSelect = document.getElementById('subType');
    const urlGroup = document.getElementById('subUrlGroup');
    const textGroup = document.getElementById('subTextGroup');
    const textContent = document.getElementById('subTextContent');
    
    // Auto toggle between URL and Raw Text input fields
    const toggleImportType = () => {
        if (typeSelect.value === 'text') {
            urlGroup.style.display = 'none';
            textGroup.style.display = 'block';
        } else {
            urlGroup.style.display = 'block';
            textGroup.style.display = 'none';
        }
    };
    typeSelect.removeEventListener('change', toggleImportType);
    typeSelect.addEventListener('change', toggleImportType);
    
    // Auto toggle expiration date field display
    const toggleDateGroup = () => {
        if (periodSelect.value === 'permanent') {
            dateGroup.style.display = 'none';
        } else {
            dateGroup.style.display = 'block';
        }
    };
    periodSelect.removeEventListener('change', toggleDateGroup);
    periodSelect.addEventListener('change', toggleDateGroup);

    if (idx === -1) {
        document.getElementById('modalTitle').textContent = '添加机场订阅';
        subNameInput.value = '';
        subUrlInput.value = '';
        typeSelect.value = 'url';
        textContent.value = '';
        subEnabledInput.checked = true;
        
        periodSelect.value = 'permanent';
        dateInput.value = '';
        trafficInput.value = '';
        resetInput.value = '';
    } else {
        document.getElementById('modalTitle').textContent = '编辑机场订阅';
        const sub = configData.subscriptions[idx];
        subNameInput.value = sub.name;
        subEnabledInput.checked = sub.enabled !== false;
        
        const isText = sub.type === 'text' || sub.url.includes('\n') || sub.url.startsWith('trojan://') || sub.url.startsWith('ss://');
        if (isText) {
            typeSelect.value = 'text';
            textContent.value = sub.url;
            subUrlInput.value = '';
        } else {
            typeSelect.value = 'url';
            subUrlInput.value = sub.url;
            textContent.value = '';
        }
        
        periodSelect.value = sub.expire_period || 'permanent';
        dateInput.value = (sub.expire_date && sub.expire_date !== 'never') ? sub.expire_date : '';
        trafficInput.value = sub.total_traffic_gb || '';
        resetInput.value = sub.reset_day || '';
    }
    
    toggleImportType();
    toggleDateGroup();
    subModal.classList.add('active');
}

function closeSubModal() {
    subModal.classList.remove('active');
}

btnModalSave.addEventListener('click', async () => {
    const name = subNameInput.value.trim();
    const importType = document.getElementById('subType').value;
    const url = importType === 'text' ? document.getElementById('subTextContent').value.trim() : subUrlInput.value.trim();
    const enabled = subEnabledInput.checked;
    
    const period = document.getElementById('subExpirePeriod').value;
    const expireDate = period === 'permanent' ? 'never' : (document.getElementById('subExpireDate').value || 'never');
    const totalTraffic = parseInt(document.getElementById('subTotalTraffic').value) || 0;
    const resetDay = parseInt(document.getElementById('subResetDay').value) || 0;
    
    if (!name || !url) {
        alert('机场名称及订阅内容（URL 或节点列表）不能为空！');
        return;
    }
    
    const subObj = { 
        name, 
        url, 
        type: importType,
        enabled,
        expire_period: period,
        expire_date: expireDate,
        total_traffic_gb: totalTraffic,
        reset_day: resetDay
    };
    
    if (editingSubIndex === -1) {
        // Adding new
        configData.subscriptions.push(subObj);
    } else {
        // Editing existing
        configData.subscriptions[editingSubIndex] = subObj;
    }
    
    closeSubModal();
    
    await saveConfig();
    renderSubscriptions();
});

// Clear Logs panel
btnClearLogs.addEventListener('click', () => {
    terminal.innerHTML = '';
});

// Credentials card copying
document.querySelectorAll('.credential-item').forEach(item => {
    item.addEventListener('click', () => {
        const text = item.getAttribute('data-copy');
        navigator.clipboard.writeText(text);
        
        // Visual indicator
        const originalText = item.querySelector('.cr-code').textContent;
        item.querySelector('.cr-code').textContent = '✅ 已成功复制到剪贴板！';
        item.querySelector('.cr-code').style.color = '#10b981';
        
        setTimeout(() => {
            item.querySelector('.cr-code').textContent = originalText;
            item.querySelector('.cr-code').style.color = '#6366f1';
        }, 1200);
    });
});

// ==================== SYSTEM SETTINGS CONTROLS ====================

function loadSettingsForm() {
    if (!configData) return;
    settingSmartPort.value = configData.smart_port || 1080;
    settingPortPoolStart.value = configData.port_pool_start || 20000;
    
    const auth = configData.socks5_auth || { enabled: false, username: '', password: '' };
    settingSocksEnabled.checked = auth.enabled;
    settingSocksUsername.value = auth.username || '';
    settingSocksPassword.value = auth.password || '';
    if (socksCredentialsWrapper) {
        socksCredentialsWrapper.style.display = auth.enabled ? 'block' : 'none';
    } else {
        settingSocksCredentialsGroup.style.display = auth.enabled ? 'grid' : 'none';
    }
    if (portCredentialsCard) {
        portCredentialsCard.style.display = auth.enabled ? 'block' : 'none';
    }
    
    settingStickyTTL.value = configData.sticky_session_ttl_minutes || 30;
    settingAlarmPercent.value = configData.alarm_threshold_percent || 50;
    
    const dashboardUser = document.getElementById('settingDashboardUsername');
    if (dashboardUser) {
        dashboardUser.value = configData.dashboard_username || 'admin';
    }
    const dashboardPass = document.getElementById('settingDashboardPassword');
    if (dashboardPass) {
        dashboardPass.value = configData.dashboard_password || 'admin';
    }
    
    // MFA settings card display on VPS
    const mfaCard = document.getElementById('mfaSettingsCard');
    if (mfaCard) {
        if (authInfo.auth_required && authInfo.two_factor_enabled) {
            mfaCard.style.display = 'block';
            const secret = configData.two_factor_secret || '未生成';
            const mfaSecretEl = document.getElementById('settingMfaSecret');
            const mfaUriEl = document.getElementById('settingMfaUri');
            if (mfaSecretEl) {
                mfaSecretEl.textContent = secret;
                mfaSecretEl.style.cursor = 'pointer';
                mfaSecretEl.title = '点击复制密钥';
                // Remove old listeners by cloning
                const newSecretEl = mfaSecretEl.cloneNode(true);
                mfaSecretEl.parentNode.replaceChild(newSecretEl, mfaSecretEl);
                newSecretEl.addEventListener('click', () => {
                    navigator.clipboard.writeText(secret);
                    alert('2FA 密钥已成功复制！');
                });
            }
            if (mfaUriEl) {
                mfaUriEl.textContent = `otpauth://totp/ProxyHub?secret=${secret}&issuer=ProxyHub`;
            }
        } else {
            mfaCard.style.display = 'none';
        }
    }
    
    // Render port credentials table
    renderPortCredentialsTable();
}

function renderPortCredentialsTable() {
    const portCredentialsTableBody = document.getElementById('portCredentialsTableBody');
    if (!portCredentialsTableBody) return;
    
    portCredentialsTableBody.innerHTML = '';
    
    // Render other countries sorted (GLOBAL credentials are managed in the global settings above)
    const portCreds = (configData && configData.port_credentials) || {};
    const activeCountries = Object.keys(groupedNodes).filter(c => c !== 'GLOBAL');
    const configuredCountries = Object.keys(portCreds).filter(c => c !== 'GLOBAL');
    const allCountriesSet = new Set([...activeCountries, ...configuredCountries]);
    const countries = Array.from(allCountriesSet).sort();
    
    countries.forEach(country => {
        const emoji = getCountryEmoji(country);
        const name = getCountryName(country);
        const cred = portCreds[country] || { username: '', password: '' };
        const userVal = cred.username || '';
        const passVal = cred.password || '';
        
        const tr = document.createElement('tr');
        tr.setAttribute('data-country', country);
        tr.innerHTML = `
            <td>
                <div class="country-flag-cell">
                    <span>${emoji}</span>
                    <span>${name}</span>
                    <span class="country-code-badge">${country}</span>
                </div>
            </td>
            <td>
                <input type="text" class="port-cred-user" placeholder="留空则使用全局默认" value="${userVal}">
            </td>
            <td>
                <input type="password" class="port-cred-pass" placeholder="留空则使用全局默认" value="${passVal}">
            </td>
            <td style="text-align: right; padding-right: 20px;">
                <button class="btn btn-secondary btn-sm btn-reset-port-cred" data-country="${country}">清空</button>
            </td>
        `;
        portCredentialsTableBody.appendChild(tr);
    });
    
    // Wire inline reset buttons
    portCredentialsTableBody.querySelectorAll('.btn-reset-port-cred').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            const country = btn.getAttribute('data-country');
            const tr = portCredentialsTableBody.querySelector(`tr[data-country="${country}"]`);
            if (tr) {
                tr.querySelector('.port-cred-user').value = '';
                tr.querySelector('.port-cred-pass').value = '';
            }
        });
    });
}

// URL-safe credential validator: only allows alphanumeric, hyphen, underscore, dot
function isCredentialSafe(str) {
    if (!str) return true;
    return /^[A-Za-z0-9\-_.]+$/.test(str);
}

async function savePortCredentials() {
    const portCredentialsTableBody = document.getElementById('portCredentialsTableBody');
    const btnSavePortCredentials = document.getElementById('btnSavePortCredentials');
    if (!portCredentialsTableBody || !btnSavePortCredentials) return;
    
    btnSavePortCredentials.disabled = true;
    btnSavePortCredentials.textContent = '保存配置中...';
    
    const credentials = {};
    const rows = portCredentialsTableBody.querySelectorAll('tr[data-country]');
    rows.forEach(row => {
        const country = row.getAttribute('data-country');
        const username = row.querySelector('.port-cred-user').value.trim();
        const password = row.querySelector('.port-cred-pass').value;
        
        if (username || password) {
            if (!isCredentialSafe(username) || !isCredentialSafe(password)) {
                alert(`国家 ${country} 的用户名或密码包含不安全字符！\n\n仅允许：字母、数字、连字符(-)、下划线(_)、点(.)\n不允许：@ # ! * % : / ? & = 等特殊字符\n\n原因：特殊字符会导致代理客户端 URL 解析失败。`);
                btnSavePortCredentials.disabled = false;
                btnSavePortCredentials.textContent = '保存端口配置';
                return;
            }
            credentials[country] = { username, password };
        }
    });
    
    const res = await fetchAPI('port/credentials', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ credentials })
    });
    
    if (res && res.status === 'success') {
        alert('地区中转端口独立凭据保存成功！正在重启内核生效...');
        await loadConfig();
        renderPortCredentialsTable();
        renderPortsTable();
        updateGeneratedProxyLink();
    } else {
        alert('保存独立凭据配置失败，请检查网络！');
    }
    
    btnSavePortCredentials.disabled = false;
    btnSavePortCredentials.textContent = '保存端口配置';
}

const btnSavePortCredentials = document.getElementById('btnSavePortCredentials');
if (btnSavePortCredentials) {
    btnSavePortCredentials.addEventListener('click', savePortCredentials);
}

if (settingSocksEnabled) {
    settingSocksEnabled.addEventListener('change', () => {
        const isChecked = settingSocksEnabled.checked;
        if (socksCredentialsWrapper) {
            socksCredentialsWrapper.style.display = isChecked ? 'block' : 'none';
        } else {
            settingSocksCredentialsGroup.style.display = isChecked ? 'grid' : 'none';
        }
        if (portCredentialsCard) {
            portCredentialsCard.style.display = isChecked ? 'block' : 'none';
        }
    });
}

if (btnSaveSystemSettings) {
    btnSaveSystemSettings.addEventListener('click', async () => {
        btnSaveSystemSettings.disabled = true;
        btnSaveSystemSettings.textContent = '保存配置中...';
        
        const dashboardUser = document.getElementById('settingDashboardUsername');
        const dashboardPass = document.getElementById('settingDashboardPassword');
        const oldDashboardUser = configData.dashboard_username || 'admin';
        const oldDashboardPass = configData.dashboard_password || 'admin';
        const newDashboardUser = dashboardUser ? dashboardUser.value.trim() : 'admin';
        const newDashboardPass = dashboardPass ? dashboardPass.value.trim() : 'admin';
        
        if (!newDashboardUser || !newDashboardPass) {
            alert('面板管理账户和访问密码不能为空！');
            btnSaveSystemSettings.disabled = false;
            btnSaveSystemSettings.textContent = '保存系统配置';
            return;
        }
        
        const socksUser = settingSocksUsername.value.trim();
        const socksPass = settingSocksPassword.value;
        
        if (settingSocksEnabled.checked && (!isCredentialSafe(socksUser) || !isCredentialSafe(socksPass))) {
            alert('SOCKS5 用户名或密码包含不安全字符！\n\n仅允许：字母、数字、连字符(-)、下划线(_)、点(.)\n不允许：@ # ! * % : / ? & = 等特殊字符\n\n原因：特殊字符会导致代理客户端 URL 解析失败。');
            btnSaveSystemSettings.disabled = false;
            btnSaveSystemSettings.textContent = '保存系统配置';
            return;
        }
        
        const payload = {
            smart_port: parseInt(settingSmartPort.value) || 1080,
            port_pool_start: parseInt(settingPortPoolStart.value) || 20000,
            socks5_auth_enabled: settingSocksEnabled.checked,
            socks5_auth_username: socksUser,
            socks5_auth_password: socksPass,
            sticky_session_ttl_minutes: parseInt(settingStickyTTL.value) || 30,
            alarm_threshold_percent: parseInt(settingAlarmPercent.value) || 50,
            dashboard_username: newDashboardUser,
            dashboard_password: newDashboardPass
        };
        
        const res = await fetchAPI('system/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        
        if (res && res.status === 'success') {
            if (oldDashboardUser !== newDashboardUser || oldDashboardPass !== newDashboardPass) {
                alert('面板管理凭证已成功更新！为了系统安全，将强制退出并重新身份验证！');
                localStorage.removeItem('proxyhub_token');
                window.location.reload();
                return;
            }
            alert('系统通用参数及智能网关配置更新成功！正在重新加载生效...');
            await loadConfig();
            loadSettingsForm();
        } else {
            alert('保存配置失败，请确保输入格式正确且端口未被占用！');
        }
        btnSaveSystemSettings.disabled = false;
        btnSaveSystemSettings.textContent = '保存系统配置';
    });
}

// ==================== NODES SPEED TEST ====================

if (btnPingAll) {
    btnPingAll.addEventListener('click', async () => {
        btnPingAll.disabled = true;
        btnPingAll.querySelector('span').textContent = '测速中...';
        
        // Visually mark active tags as testing
        document.querySelectorAll('.node-latency-tag').forEach(tag => {
            tag.textContent = '...';
            tag.className = 'node-latency-tag latency-timeout';
        });
        
        const res = await fetchAPI('nodes/ping', { method: 'POST' });
        if (res && res.status === 'success') {
            nodeLatencies = res.delays;
            renderNodesTabContent();
        } else {
            alert('全局测速失败，请检查核心服务是否已启动！');
        }
        
        btnPingAll.disabled = false;
        btnPingAll.querySelector('span').textContent = '一键测速';
    });
}

// ==================== HELPERS ====================

function getCountryEmoji(countryCode) {
    const emojiMap = {
        HK: '🇭🇰', US: '🇺🇸', JP: '🇯🇵', SG: '🇸🇬', TW: '🇹🇼', 
        KR: '🇰🇷', UK: '🇬🇧', DE: '🇩🇪', FR: '🇫🇷', CA: '🇨🇦', 
        AU: '🇦🇺', RU: '🇷🇺', CN: '🇨🇳', GLOBAL: '◈', Others: '🏳️'
    };
    return emojiMap[countryCode] || '🏳️';
}

function getCountryName(countryCode) {
    const nameMap = {
        HK: '中国香港', US: '美国', JP: '日本', SG: '新加坡', TW: '中国台湾', 
        KR: '韩国', UK: '英国', DE: '德国', FR: '法国', CA: '加拿大', 
        AU: '澳大利亚', RU: '俄罗斯', CN: '中国大陆', GLOBAL: '全局代理', Others: '其他地区'
    };
    return nameMap[countryCode] || '其他/混合';
}

// ==================== INITIALIZATION ====================

const loginContainer = document.getElementById('loginContainer');
const loginPassword = document.getElementById('loginPassword');
const btnLoginSubmit = document.getElementById('btnLoginSubmit');
const loginErrorMessage = document.getElementById('loginErrorMessage');
const appMainContainer = document.getElementById('appMainContainer');
const btnLogoutSystem = document.getElementById('btnLogoutSystem');

// Helper to show login cover screen
function showLoginScreen(errorMsg = '') {
    if (loginContainer) {
        loginContainer.style.display = 'flex';
        setTimeout(() => {
            loginContainer.style.opacity = '1';
            loginContainer.querySelector('.login-card').style.transform = 'translateY(0)';
        }, 50);
    }
    if (appMainContainer) {
        appMainContainer.style.filter = 'blur(15px)';
        appMainContainer.style.pointerEvents = 'none';
    }
    if (loginErrorMessage) {
        loginErrorMessage.textContent = errorMsg;
    }
    if (loginPassword) {
        loginPassword.value = '';
        loginPassword.focus();
    }
}

// Helper to hide login cover screen with sleek slide-out
function hideLoginScreen() {
    if (loginContainer) {
        loginContainer.style.opacity = '0';
        loginContainer.querySelector('.login-card').style.transform = 'translateY(-20px)';
        setTimeout(() => {
            loginContainer.style.display = 'none';
        }, 500);
    }
    if (appMainContainer) {
        appMainContainer.style.filter = 'none';
        appMainContainer.style.pointerEvents = 'auto';
    }
}

async function handleLoginSubmit() {
    const loginUsername = document.getElementById('loginUsername');
    const loginTotp = document.getElementById('loginTotp');
    
    const username = loginUsername ? loginUsername.value.trim() : '';
    const password = loginPassword.value;
    const totpCode = loginTotp ? loginTotp.value.trim() : '';
    
    if (!username) {
        loginErrorMessage.textContent = '请输入管理员账号！';
        return;
    }
    if (!password) {
        loginErrorMessage.textContent = '请输入访问密码！';
        return;
    }
    if (authInfo.two_factor_enabled && !totpCode) {
        loginErrorMessage.textContent = '请输入 2FA 动态验证码！';
        return;
    }
    
    btnLoginSubmit.disabled = true;
    btnLoginSubmit.querySelector('span').textContent = '验证中...';
    
    try {
        const response = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password, totp_code: totpCode })
        });
        
        if (response.ok) {
            const data = await response.json();
            localStorage.setItem('proxyhub_token', data.token);
            loginErrorMessage.textContent = '';
            hideLoginScreen();
            
            // Trigger core system initialization on first login success
            await startAppSystem();
        } else {
            const data = await response.json().catch(() => ({}));
            loginErrorMessage.textContent = data.detail || '用户名、密码或动态验证码错误！';
            
            // Play simple shake feedback animation
            const card = loginContainer.querySelector('.login-card');
            card.classList.remove('login-error-msg');
            void card.offsetWidth; // trigger reflow
            card.classList.add('login-error-msg');
        }
    } catch (e) {
        loginErrorMessage.textContent = '网络连接失败，请检查后端服务是否启动！';
    } finally {
        btnLoginSubmit.disabled = false;
        btnLoginSubmit.querySelector('span').textContent = '确认登录';
    }
}

async function checkAuthentication() {
    // 1. Fetch environment authentication info
    try {
        const infoRes = await fetch('/api/auth/info');
        if (infoRes.ok) {
            authInfo = await infoRes.json();
            if (!authInfo.auth_required) {
                // Windows mode: Bypassed!
                hideLoginScreen();
                const logoutBtn = document.getElementById('btnLogoutSystem');
                if (logoutBtn) {
                    logoutBtn.style.color = 'var(--text-muted)';
                    logoutBtn.style.borderColor = 'var(--glass-border)';
                    logoutBtn.style.background = 'transparent';
                    logoutBtn.disabled = true;
                    logoutBtn.querySelector('span').textContent = 'Windows 免密运行中';
                }
                await startAppSystem();
                return true;
            }
            
            // VPS mode: Adapt login UI to 2FA
            const loginUsernameGroup = document.getElementById('loginUsername')?.closest('.form-group');
            if (loginUsernameGroup) {
                loginUsernameGroup.style.display = 'block';
            }
            const loginPasswordGroup = document.getElementById('loginPassword')?.closest('.form-group');
            if (loginPasswordGroup) {
                loginPasswordGroup.style.display = 'block';
            }
            
            const totpGroup = document.getElementById('loginTotpGroup');
            if (totpGroup) {
                if (authInfo.two_factor_enabled) {
                    totpGroup.style.display = 'block';
                } else {
                    totpGroup.style.display = 'none';
                }
            }
            
            const loginBrandP = document.querySelector('.login-brand p');
            if (loginBrandP) {
                loginBrandP.textContent = '多因子安全访问控制 (Multi-Factor Authentication)';
            }
        }
    } catch (e) {
        console.error('Failed to fetch auth info:', e);
    }

    const token = localStorage.getItem('proxyhub_token');
    if (!token) {
        showLoginScreen();
        return false;
    }
    
    // Call quick verify route
    try {
        const response = await fetch('/api/auth/verify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token })
        });
        
        if (response.ok) {
            const data = await response.json();
            if (data.status === 'success' && data.valid) {
                hideLoginScreen();
                await startAppSystem();
                return true;
            }
        }
        localStorage.removeItem('proxyhub_token');
        showLoginScreen('身份凭证已失效，请重新验证！');
        return false;
    } catch (e) {
        showLoginScreen('网络服务异常，正处于离线安全保护中！');
        return false;
    }
}

// Separate actual core app bootstrap
let appInitialized = false;
async function startAppSystem() {
    if (appInitialized) return;
    appInitialized = true;
    
    await loadConfig();
    await pollStatus();
    await loadNodesData();
    initGenerator();
    
    // Periodically poll status
    setInterval(pollStatus, 2000);
    
    // Periodically refresh node ports and active counts
    setInterval(loadNodesData, 5000);
}

// Bind auth UI listeners on boot
function initAuthGate() {
    if (btnLoginSubmit) {
        btnLoginSubmit.addEventListener('click', handleLoginSubmit);
    }
    if (loginPassword) {
        loginPassword.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') handleLoginSubmit();
        });
    }
    const loginUsername = document.getElementById('loginUsername');
    if (loginUsername) {
        loginUsername.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') handleLoginSubmit();
        });
    }
    const loginTotp = document.getElementById('loginTotp');
    if (loginTotp) {
        loginTotp.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') handleLoginSubmit();
        });
    }
    if (btnLogoutSystem) {
        btnLogoutSystem.addEventListener('click', () => {
            localStorage.removeItem('proxyhub_token');
            showLoginScreen('已成功安全退出！');
        });
    }
    
    // Run authentication verify
    checkAuthentication();
}

document.addEventListener('DOMContentLoaded', initAuthGate);

// ==================== DYNAMIC PROXY LINK GENERATOR ====================

let genSelectedTab = 'url'; // 'url' or 'split'

function initGenerator() {
    const protoRadios = document.querySelectorAll('input[name="genProto"]');
    const modeRadios = document.querySelectorAll('input[name="genMode"]');
    const genStickyGroup = document.getElementById('genStickyGroup');
    const genSessionId = document.getElementById('genSessionId');
    const btnGenSessionReset = document.getElementById('btnGenSessionReset');
    const genCountry = document.getElementById('genCountry');

    const tabGenUrl = document.getElementById('tabGenUrl');
    const tabGenSplit = document.getElementById('tabGenSplit');
    const genUrlBox = document.getElementById('genUrlBox');
    const genSplitBox = document.getElementById('genSplitBox');
    
    if (!tabGenUrl) return; // Guard clause
    
    // Wire tab selectors
    tabGenUrl.addEventListener('click', () => {
        genSelectedTab = 'url';
        tabGenUrl.style.color = 'hsl(var(--accent-indigo))';
        tabGenUrl.style.borderBottom = '2px solid hsl(var(--accent-indigo))';
        tabGenSplit.style.color = 'var(--text-secondary)';
        tabGenSplit.style.borderBottom = 'none';
        genUrlBox.style.display = 'block';
        genSplitBox.style.display = 'none';
    });
    
    tabGenSplit.addEventListener('click', () => {
        genSelectedTab = 'split';
        tabGenSplit.style.color = 'hsl(var(--accent-indigo))';
        tabGenSplit.style.borderBottom = '2px solid hsl(var(--accent-indigo))';
        tabGenUrl.style.color = 'var(--text-secondary)';
        tabGenUrl.style.borderBottom = 'none';
        genUrlBox.style.display = 'none';
        genSplitBox.style.display = 'block';
    });
    
    // Mode toggles
    modeRadios.forEach(radio => {
        radio.addEventListener('change', () => {
            if (radio.value === 'sticky') {
                genStickyGroup.style.display = 'flex';
            } else {
                genStickyGroup.style.display = 'none';
            }
            updateGeneratedProxyLink();
        });
    });
    
    protoRadios.forEach(radio => {
        radio.addEventListener('change', updateGeneratedProxyLink);
    });
    
    // Session reset button
    btnGenSessionReset.addEventListener('click', () => {
        const randStr = Math.random().toString(36).substring(2, 8);
        genSessionId.value = 'sess_' + randStr;
        updateGeneratedProxyLink();
    });
    
    genSessionId.addEventListener('input', updateGeneratedProxyLink);
    genCountry.addEventListener('change', updateGeneratedProxyLink);

    // Copy buttons
    document.getElementById('btnCopyGenUrl').addEventListener('click', () => {
        navigator.clipboard.writeText(document.getElementById('genUrlResult').textContent);
        alert('已成功复制生成的代理 URL 链接！');
    });
    
    document.getElementById('btnCopyGenCurl').addEventListener('click', () => {
        navigator.clipboard.writeText(document.getElementById('genCurlResult').textContent);
        alert('已成功复制 curl 命令示例！');
    });
    
    document.getElementById('btnCopySplitHost').addEventListener('click', () => {
        const currentHost = window.location.hostname || '127.0.0.1';
        navigator.clipboard.writeText(currentHost);
        alert(`已复制主机地址: ${currentHost}`);
    });
    
    document.getElementById('btnCopySplitPort').addEventListener('click', () => {
        navigator.clipboard.writeText(document.getElementById('genSplitPort').textContent);
        alert('已复制代理端口！');
    });
    
    document.getElementById('btnCopySplitUser').addEventListener('click', () => {
        navigator.clipboard.writeText(document.getElementById('genSplitUser').textContent);
        alert('已复制用户名！');
    });
    
    document.getElementById('btnCopySplitPass').addEventListener('click', () => {
        navigator.clipboard.writeText(document.getElementById('genSplitPass').textContent);
        alert('已复制密码！');
    });
    
    // Initial update
    updateGeneratedProxyLink();
}

function populateGeneratorCountries() {
    const genCountry = document.getElementById('genCountry');
    if (!genCountry) return;
    
    const currentValue = genCountry.value;
    genCountry.innerHTML = '';
    
    // Add GLOBAL Option
    const optGlobal = document.createElement('option');
    optGlobal.value = 'GLOBAL';
    optGlobal.textContent = '◈ GLOBAL (全球自动分流)';
    genCountry.appendChild(optGlobal);
    
    // Add other active country groups
    const countries = Object.keys(groupedNodes).filter(c => c !== 'GLOBAL');
    countries.forEach(c => {
        const opt = document.createElement('option');
        opt.value = c;
        opt.textContent = `${getCountryEmoji(c)} ${c} ${getCountryName(c)}`;
        genCountry.appendChild(opt);
    });
    
    // Restore value if still present
    if ([...genCountry.options].some(o => o.value === currentValue)) {
        genCountry.value = currentValue;
    }
}

function updateGeneratedProxyLink() {
    const genProtoRadio = document.querySelector('input[name="genProto"]:checked');
    const genModeRadio = document.querySelector('input[name="genMode"]:checked');
    if (!genProtoRadio || !genModeRadio) return;

    const genProto = genProtoRadio.value;
    const genMode = genModeRadio.value;
    const genSessionId = document.getElementById('genSessionId').value.trim();
    const genCountry = document.getElementById('genCountry').value;
    
    const globalAuthUser = (configData && configData.socks5_auth && configData.socks5_auth.username) || '';
    const globalAuthPass = (configData && configData.socks5_auth && configData.socks5_auth.password) || '';
    const authEnabled = (configData && configData.socks5_auth && configData.socks5_auth.enabled) || false;
    const smartPort = (configData && configData.smart_port) || 1080;
    
    // Resolve credentials based on selected country override
    const portCreds = (configData && configData.port_credentials) || {};
    const countryCred = portCreds[genCountry] || {};
    
    const c_u = countryCred.username ? countryCred.username.trim() : '';
    const c_p = countryCred.password ? countryCred.password : '';
    
    const resolvedUserPrefix = c_u || globalAuthUser;
    const resolvedPassword = c_p || globalAuthPass;
    
    // Construct routing part of username
    let routePart = '';
    if (genCountry === 'GLOBAL') {
        routePart = genMode === 'sticky' ? `GLOBAL-sticky-${genSessionId || 'sess'}` : 'GLOBAL-rotate';
    } else {
        routePart = genMode === 'sticky' ? `${genCountry}-sticky-${genSessionId || 'sess'}` : `${genCountry}-rotate`;
    }
    
    // Build full username: prefix-route (auth enabled) or just route (no auth)
    let user = '';
    if (authEnabled && resolvedUserPrefix) {
        user = `${resolvedUserPrefix}-${routePart}`;
    } else {
        user = routePart;
    }
    
    const currentHost = window.location.hostname || '127.0.0.1';
    
    // Generate URL Result
    let proxyUrl = '';
    if (authEnabled && resolvedPassword) {
        proxyUrl = `${genProto}://${user}:${resolvedPassword}@${currentHost}:${smartPort}`;
    } else {
        proxyUrl = `${genProto}://${user}:anypass@${currentHost}:${smartPort}`;
    }
    document.getElementById('genUrlResult').textContent = proxyUrl;
    
    // Generate Split Fields
    document.getElementById('genSplitProto').textContent = genProto;
    document.getElementById('genSplitPort').textContent = smartPort;
    document.getElementById('genSplitUser').textContent = user;
    document.getElementById('genSplitPass').textContent = authEnabled ? resolvedPassword : '(任意值)';
    
    // Generate Curl Result
    document.getElementById('genCurlResult').textContent = `curl -x "${proxyUrl}" https://ipinfo.io`;
}


// ==================== THEME SYSTEM ====================
const btnThemeToggle = document.getElementById('btnThemeToggle');
const sunIcon = btnThemeToggle ? btnThemeToggle.querySelector('.theme-icon-sun') : null;
const moonIcon = btnThemeToggle ? btnThemeToggle.querySelector('.theme-icon-moon') : null;

function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('theme', theme);
    if (!sunIcon || !moonIcon) return;
    if (theme === 'dark') {
        sunIcon.style.display = 'block';
        moonIcon.style.display = 'none';
    } else {
        sunIcon.style.display = 'none';
        moonIcon.style.display = 'block';
    }
}

// Initial theme load (default to light mode)
const savedTheme = localStorage.getItem('theme') || 'light';
applyTheme(savedTheme);

if (btnThemeToggle) {
    btnThemeToggle.addEventListener('click', () => {
        const currentTheme = document.documentElement.getAttribute('data-theme') || 'light';
        const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
        applyTheme(newTheme);
    });
}
