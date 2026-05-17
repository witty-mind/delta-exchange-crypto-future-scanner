import re

with open('templates/index.html', 'r') as f:
    html = f.read()

# 1. Add CSS
css_to_add = """
        .tabs {
            display: flex;
            gap: 8px;
            margin-bottom: 16px;
            overflow-x: auto;
            padding-bottom: 4px;
        }
        .tab-btn {
            font-family: inherit;
            font-size: 0.85rem;
            font-weight: 600;
            padding: 8px 16px;
            border-radius: 999px;
            border: 1px solid var(--border);
            background: transparent;
            color: var(--muted);
            cursor: pointer;
            white-space: nowrap;
            transition: all 0.2s;
        }
        .tab-btn:hover {
            color: var(--text);
            background: var(--surface-hover);
        }
        .tab-btn.active {
            background: var(--text);
            color: var(--bg);
            border-color: transparent;
        }
        html.light .tab-btn.active {
            background: var(--blue);
            color: #fff;
        }
        .scanner-grid .card[data-group] {
            display: none;
        }
        .scanner-grid .card[data-group].show {
            display: flex;
            animation: tabEnter 0.4s cubic-bezier(0.22, 1, 0.36, 1) forwards;
        }
        .scanner-grid .card-wide {
            grid-column: 1 / -1;
        }
        @keyframes tabEnter {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }
"""
html = html.replace("    </style>", css_to_add + "    </style>")

# 2. Add Tabs HTML
tabs_html = """
        </section>

        <nav class="tabs">
            <button class="tab-btn active" data-target="all">All Futures</button>
            <button class="tab-btn" data-target="pdh">PDH/PDL</button>
            <button class="tab-btn" data-target="ema">EMA Stack</button>
            <button class="tab-btn" data-target="ichi">Ichimoku</button>
        </nav>

        <div class="main-grid">
"""
html = html.replace('        </section>\n\n        <div class="main-grid">', tabs_html)

# 3. Add data-groups to cards
def add_group(html, h2_text, group_name, extra_class=""):
    pattern = r'(<div class="card">\s*<div class="card-head">\s*(?:<div[^>]*>\s*)?<h2>\s*' + re.escape(h2_text) + r')'
    def repl(m):
        return m.group(0).replace('<div class="card">', f'<div class="card{extra_class}" data-group="{group_name}">', 1)
    return re.sub(pattern, repl, html, count=1)

html = add_group(html, "Running Anove PDH", "pdh")
html = add_group(html, "Running below PDL", "pdh")
html = add_group(html, "EMA Pullback Buy", "ema")
html = add_group(html, "EMA Pullback Sell", "ema")
html = add_group(html, "PDH + EMA stack", "pdh", " card-wide")
html = add_group(html, "Ichimoku stack", "ichi")
html = add_group(html, "Ichimoku bear", "ichi")

def repl_classic_bull(m):
    return m.group(0).replace('<div class="card">', '<div class="card" data-group="ichi">', 1)
html = re.sub(r'(<div class="card">\s*<div class="card-head">\s*<div[^>]*>\s*<h2>Ichimoku Classic\s*<span[^>]*>&#9650;\s*Bullish)', repl_classic_bull, html, count=1)

def repl_classic_bear(m):
    return m.group(0).replace('<div class="card">', '<div class="card" data-group="ichi">', 1)
html = re.sub(r'(<div class="card">\s*<div class="card-head">\s*<div[^>]*>\s*<h2>Ichimoku Classic\s*<span[^>]*>&#9660;\s*Bearish)', repl_classic_bear, html, count=1)

# 4. Move futures-section into scanner-grid
futures_section_match = re.search(r'<section class="futures-section"[^>]*>(.*?)</section>', html, re.DOTALL)
if futures_section_match:
    futures_inner = futures_section_match.group(1).strip()
    futures_inner = futures_inner.replace('<div class="card">', '<div class="card card-wide show" data-group="all">', 1)
    html = re.sub(r'<section class="futures-section"[^>]*>\s*.*?\s*</section>', '', html, flags=re.DOTALL)
    html = html.replace('<div class="scanner-grid">', f'<div class="scanner-grid">\n                {futures_inner}')

# 5. Add JS logic
js_to_add = """
        /* ── Tabs toggle ────────────────────────────────── */
        (function() {
            const tabs = document.querySelectorAll('.tab-btn');
            const cards = document.querySelectorAll('.scanner-grid .card[data-group]');
            
            // Initialize: show active tab
            cards.forEach(card => {
                if (card.dataset.group === 'all') {
                    card.style.display = 'flex';
                    card.classList.add('show');
                } else {
                    card.style.display = 'none';
                    card.classList.remove('show');
                }
            });

            tabs.forEach(tab => {
                tab.addEventListener('click', () => {
                    tabs.forEach(t => t.classList.remove('active'));
                    tab.classList.add('active');
                    
                    const targetGroup = tab.dataset.target;
                    
                    cards.forEach(card => {
                        if (card.dataset.group === targetGroup) {
                            card.style.display = 'flex';
                            void card.offsetWidth; // Reflow
                            card.classList.add('show');
                        } else {
                            card.style.display = 'none';
                            card.classList.remove('show');
                        }
                    });
                });
            });
        })();
"""
html = html.replace("    </script>\n</body>", js_to_add + "    </script>\n</body>")

with open('templates/index.html', 'w') as f:
    f.write(html)
print("Done")
