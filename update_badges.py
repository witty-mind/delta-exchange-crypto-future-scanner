import re

with open('templates/index.html', 'r') as f:
    html = f.read()

# 1. Update Tabs HTML
old_tabs = """        <nav class="tabs">
            <button class="tab-btn active" data-target="all">All Futures</button>
            <button class="tab-btn" data-target="pdh">PDH/PDL</button>
            <button class="tab-btn" data-target="ema">EMA Stack</button>
            <button class="tab-btn" data-target="ichi">Ichimoku</button>
        </nav>"""

new_tabs = """        <nav class="tabs">
            <button class="tab-btn active" data-target="all">All Futures</button>
            <button class="tab-btn" data-target="pdh">
                PDH/PDL <span class="tab-badge" id="badge-pdh" style="display:none">0</span>
            </button>
            <button class="tab-btn" data-target="ema">
                EMA Stack <span class="tab-badge" id="badge-ema" style="display:none">0</span>
            </button>
            <button class="tab-btn" data-target="ichi">
                Ichimoku <span class="tab-badge" id="badge-ichi" style="display:none">0</span>
            </button>
        </nav>"""
html = html.replace(old_tabs, new_tabs)

# 2. Add Tab Badge CSS
css_addition = """
        .tab-badge {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            background: var(--blue);
            color: #fff;
            font-size: 0.65rem;
            font-weight: 700;
            padding: 0 4px;
            min-width: 16px;
            height: 16px;
            border-radius: 999px;
            margin-left: 6px;
            line-height: 1;
        }
"""
html = html.replace("    </style>", css_addition + "    </style>")

# 3. Add JS tracking globals
js_globals = """
        const prevSymbols = {};
        const unreadCounts = { pdh: 0, ema: 0, ichi: 0 };
        const urlToTab = {
            '/above-pdh': 'pdh',
            '/below-pdl': 'pdh',
            '/pdh-ema-confluence': 'ema',
            '/ema-pullback-buy': 'ema',
            '/ema-pullback-sell': 'ema',
            '/ichimoku-stack': 'ichi',
            '/ichimoku-bear': 'ichi',
            '/ichimoku-classic-bull': 'ichi',
            '/ichimoku-classic-bear': 'ichi'
        };

        function updateBadge(tab) {
            let badge = document.getElementById('badge-' + tab);
            if (badge) {
                if (unreadCounts[tab] > 0) {
                    badge.textContent = unreadCounts[tab];
                    badge.style.display = 'inline-flex';
                } else {
                    badge.style.display = 'none';
                }
            }
        }
"""
html = html.replace("        function load(url, cb, errCb) {", js_globals + "\n        function load(url, cb, errCb) {")

# 4. Modify load function to track diffs
old_load = """        function load(url, cb, errCb) {
            const ctrl = new AbortController();
            const t = setTimeout(() => ctrl.abort(), 18000);
            fetch(url, { signal: ctrl.signal })
                .then(r => {
                    clearTimeout(t);
                    if (!r.ok) throw new Error(String(r.status));
                    return r.json();
                })
                .then(cb)
                .catch(() => {
                    clearTimeout(t);
                    if (errCb) errCb();
                });
        }"""

new_load = """        function load(url, cb, errCb) {
            const ctrl = new AbortController();
            const t = setTimeout(() => ctrl.abort(), 18000);
            fetch(url, { signal: ctrl.signal })
                .then(r => {
                    clearTimeout(t);
                    if (!r.ok) throw new Error(String(r.status));
                    return r.json();
                })
                .then(res => {
                    if (res && res.data && urlToTab[url]) {
                        let currentSymbols = new Set(res.data.map(d => d.symbol));
                        if (prevSymbols[url]) {
                            let added = 0;
                            currentSymbols.forEach(sym => {
                                if (!prevSymbols[url].has(sym)) added++;
                            });
                            if (added > 0) {
                                let tab = urlToTab[url];
                                let btn = document.querySelector(`.tab-btn[data-target="${tab}"]`);
                                if (btn && !btn.classList.contains('active')) {
                                    unreadCounts[tab] += added;
                                    updateBadge(tab);
                                }
                            }
                        }
                        prevSymbols[url] = currentSymbols;
                    }
                    cb(res);
                })
                .catch(() => {
                    clearTimeout(t);
                    if (errCb) errCb();
                });
        }"""
html = html.replace(old_load, new_load)

# 5. Clear badge on click
# We need to insert badge clearing into the tab click listener
tab_click_search = """                    const targetGroup = tab.dataset.target;"""
tab_click_replace = """                    const targetGroup = tab.dataset.target;
                    
                    if (unreadCounts[targetGroup] !== undefined) {
                        unreadCounts[targetGroup] = 0;
                        updateBadge(targetGroup);
                    }"""
html = html.replace(tab_click_search, tab_click_replace)

with open('templates/index.html', 'w') as f:
    f.write(html)
print("Done")
