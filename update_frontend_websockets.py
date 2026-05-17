import re

with open('templates/index.html', 'r') as f:
    html = f.read()

# 1. Remove renderFutures function (we will inline it)
html = re.sub(r'function renderFutures\(\)\s*\{.*?\n        \}\n', '', html, flags=re.DOTALL)

# 2. Remove load() and refresh() functions
# The block spans from `function load(url, cb, errCb) {` to `setInterval(refresh, 10000);\n        refresh();`
match = re.search(r'function load\(url, cb, errCb\).*?setInterval\(refresh, 10000\);\s*refresh\(\);', html, flags=re.DOTALL)
if match:
    html = html.replace(match.group(0), "")
else:
    print("Could not find load/refresh block")

# 3. Add the websocket listener
ws_listener = """
            socket.on('scan_results', fullPayload => {
                function updateTabBadge(urlKey, newSymbolsList) {
                    const tab = urlToTab[urlKey];
                    if (!tab) return;
                    let currentSymbols = new Set(newSymbolsList.map(d => d.symbol));
                    if (prevSymbols[urlKey]) {
                        let added = 0;
                        currentSymbols.forEach(sym => {
                            if (!prevSymbols[urlKey].has(sym)) added++;
                        });
                        if (added > 0) {
                            let btn = document.querySelector(`.tab-btn[data-target="${tab}"]`);
                            if (btn && !btn.classList.contains('active')) {
                                unreadCounts[tab] += added;
                                updateBadge(tab);
                            }
                        }
                    }
                    prevSymbols[urlKey] = currentSymbols;
                }

                // Breadth
                const b = fullPayload.market_breadth || {};
                document.getElementById('gainers-label').textContent = 'Gainers ' + (b.gainers_pct || 0) + '%';
                document.getElementById('losers-label').textContent = 'Losers ' + (b.losers_pct || 0) + '%';
                document.getElementById('gainers-bar').style.width = (b.gainers_pct || 0) + '%';
                document.getElementById('losers-bar').style.width = (b.losers_pct || 0) + '%';
                document.getElementById('total-count').textContent = (b.total || 0) + ' live perpetuals';

                // Futures
                const futures = fullPayload.futures || [];
                if (!futures.length) {
                    document.getElementById('futures-body').innerHTML = emptyRows(5, 'No perpetual list yet — is the server running and able to reach Delta?');
                } else {
                    const list = reverseSort ? [...futures].reverse() : [...futures];
                    document.getElementById('futures-body').innerHTML = list.map(d => {
                        const pend = d.pending ? ' style="opacity:0.72"' : '';
                        const ch = d.change_24h;
                        const hasCh = ch !== null && ch !== undefined && !Number.isNaN(Number(ch));
                        const cls = !hasCh ? '' : ch > 0 ? 'up' : ch < 0 ? 'down' : '';
                        const sign = hasCh && ch > 0 ? '+' : '';
                        const pct = hasCh ? `${sign}${fmt(ch, 2)}%` : '—';
                        return `<tr${pend}>
                            <td>${d.symbol}${d.pending ? ' <span style="font-size:0.65rem;color:var(--muted)">(prices soon)</span>' : ''}</td>
                            <td class="mono">${fmt(d.mark_price, 4)}</td>
                            <td class="mono ${cls}">${pct}</td>
                            <td class="mono">${fmt(d.high_24h, 4)}</td>
                            <td class="mono">${fmt(d.low_24h, 4)}</td>
                        </tr>`;
                    }).join('');
                }

                // Above PDH
                const pdh = fullPayload.above_pdh || [];
                updateTabBadge('/above-pdh', pdh);
                const pdhRows = pdh.length
                    ? pdh.slice(0, 20).map(d => `<tr><td>${d.symbol}</td><td class="mono">${fmt(d.mark_price, 4)}</td><td class="up">+${fmt(d.pct_above)}%</td></tr>`).join('')
                    : emptyRows(3, 'No matches');
                document.getElementById('pdh-count').textContent = pdh.length;
                document.getElementById('pdh-time').textContent = nowTime();
                document.getElementById('pdh-body').innerHTML = pdhRows;

                // Below PDL
                const pdl = fullPayload.below_pdl || [];
                updateTabBadge('/below-pdl', pdl);
                const pdlRows = pdl.length
                    ? pdl.slice(0, 20).map(d => `<tr><td>${d.symbol}</td><td class="mono">${fmt(d.mark_price, 4)}</td><td class="down">−${fmt(d.pct_below)}%</td></tr>`).join('')
                    : emptyRows(3, 'No matches');
                document.getElementById('pdl-count').textContent = pdl.length;
                document.getElementById('pdl-time').textContent = nowTime();
                document.getElementById('pdl-body').innerHTML = pdlRows;

                // EMA Buy
                const emaBuy = fullPayload.ema_pullback_buy || [];
                updateTabBadge('/ema-pullback-buy', emaBuy);
                const emaBuyRows = emaBuy.length
                    ? emaBuy.slice(0, 20).map(d => `<tr>
                        <td>${d.symbol}</td>
                        <td class="mono up">${fmt(d.mark_price, 4)}</td>
                        <td><span class="tag" style="background:var(--green-dim);color:var(--green);border:1px solid var(--green)44">${d.candle_pattern}</span></td>
                        <td class="mono">${fmt(d.ema_10, 4)}</td>
                    </tr>`).join('')
                    : emptyRows(4, 'No pullback buy signals on last closed 5m bar');
                document.getElementById('ema-pb-buy-count').textContent = emaBuy.length;
                document.getElementById('ema-pb-buy-time').textContent = nowTime();
                document.getElementById('ema-pb-buy-body').innerHTML = emaBuyRows;

                // EMA Sell
                const emaSell = fullPayload.ema_pullback_sell || [];
                updateTabBadge('/ema-pullback-sell', emaSell);
                const emaSellRows = emaSell.length
                    ? emaSell.slice(0, 20).map(d => `<tr>
                        <td>${d.symbol}</td>
                        <td class="mono down">${fmt(d.mark_price, 4)}</td>
                        <td><span class="tag" style="background:var(--red-dim);color:var(--red);border:1px solid var(--red)44">${d.candle_pattern}</span></td>
                        <td class="mono">${fmt(d.ema_10, 4)}</td>
                    </tr>`).join('')
                    : emptyRows(4, 'No pullback sell signals on last closed 5m bar');
                document.getElementById('ema-pb-sell-count').textContent = emaSell.length;
                document.getElementById('ema-pb-sell-time').textContent = nowTime();
                document.getElementById('ema-pb-sell-body').innerHTML = emaSellRows;

                // Confluence
                const conf = fullPayload.pdh_ema_confluence || [];
                updateTabBadge('/pdh-ema-confluence', conf);
                const confRows = conf.length
                    ? conf.slice(0, 20).map(d => `<tr>
                        <td>${d.symbol}</td>
                        <td class="mono">${fmt(d.mark_price, 4)}</td>
                        <td class="up">+${fmt(d.pct_above_100)}%</td>
                    </tr>`).join('')
                    : emptyRows(3, 'No matches');
                document.getElementById('conf-count').textContent = conf.length;
                document.getElementById('conf-time').textContent = nowTime();
                document.getElementById('conf-body').innerHTML = confRows;

                // Ichi Stack
                const ichiStack = fullPayload.ichimoku_stack || [];
                updateTabBadge('/ichimoku-stack', ichiStack);
                const ichiStackRows = ichiStack.length
                    ? ichiStack.slice(0, 20).map(d => `<tr>
                        <td>${d.symbol}</td>
                        <td class="mono">${fmt(d.close, 4)}</td>
                        <td class="mono up">${fmt(d.rvol, 2)}</td>
                        <td class="mono">${fmt(d.tenkan, 4)} / ${fmt(d.kijun, 4)}</td>
                    </tr>`).join('')
                    : emptyRows(4, 'No matches (last closed 5m bar: cloud + TK + lagging + RVOL + OI; needs OI: history)');
                document.getElementById('ichi-count').textContent = ichiStack.length;
                document.getElementById('ichi-time').textContent = nowTime();
                document.getElementById('ichi-body').innerHTML = ichiStackRows;

                // Ichi Bear
                const ichiBear = fullPayload.ichimoku_bear || [];
                updateTabBadge('/ichimoku-bear', ichiBear);
                const ichiBearRows = ichiBear.length
                    ? ichiBear.slice(0, 20).map(d => `<tr>
                        <td>${d.symbol}</td>
                        <td class="mono">${fmt(d.close, 4)}</td>
                        <td class="mono down">${fmt(d.rvol, 2)}</td>
                        <td class="mono">${fmt(d.tenkan, 4)} / ${fmt(d.kijun, 4)}</td>
                    </tr>`).join('')
                    : emptyRows(4, 'No matches (last closed 5m bar: below cloud + TK↓ + lagging↓ + RVOL + OI; needs OI: history)');
                document.getElementById('ichi-bear-count').textContent = ichiBear.length;
                document.getElementById('ichi-bear-time').textContent = nowTime();
                document.getElementById('ichi-bear-body').innerHTML = ichiBearRows;

                // Ichi Classic Bull
                const cbull = fullPayload.ichimoku_classic_bull || [];
                updateTabBadge('/ichimoku-classic-bull', cbull);
                const cbullRows = cbull.length
                    ? cbull.slice(0, 30).map(d => `<tr>
                        <td>${d.symbol}</td>
                        <td class="mono up">${fmt(d.close, 4)}</td>
                        <td class="mono">${fmt(d.tenkan, 4)} / ${fmt(d.kijun, 4)}</td>
                        <td class="mono" style="color:var(--green)">${fmt(d.senkou_a, 4)}</td>
                        <td class="mono" style="color:var(--green)">${fmt(d.senkou_b, 4)}</td>
                    </tr>`).join('')
                    : emptyRows(5, 'No matches — waiting for TK crossover ↑ + Chikou above + cloud breakout');
                document.getElementById('ichi-classic-bull-count').textContent = cbull.length;
                document.getElementById('ichi-classic-bull-time').textContent = nowTime();
                document.getElementById('ichi-classic-bull-body').innerHTML = cbullRows;

                // Ichi Classic Bear
                const cbear = fullPayload.ichimoku_classic_bear || [];
                updateTabBadge('/ichimoku-classic-bear', cbear);
                const cbearRows = cbear.length
                    ? cbear.slice(0, 30).map(d => `<tr>
                        <td>${d.symbol}</td>
                        <td class="mono down">${fmt(d.close, 4)}</td>
                        <td class="mono">${fmt(d.tenkan, 4)} / ${fmt(d.kijun, 4)}</td>
                        <td class="mono" style="color:var(--red)">${fmt(d.senkou_a, 4)}</td>
                        <td class="mono" style="color:var(--red)">${fmt(d.senkou_b, 4)}</td>
                    </tr>`).join('')
                    : emptyRows(5, 'No matches — waiting for TK crossover ↓ + Chikou below + cloud breakdown');
                document.getElementById('ichi-classic-bear-count').textContent = cbear.length;
                document.getElementById('ichi-classic-bear-time').textContent = nowTime();
                document.getElementById('ichi-classic-bear-body').innerHTML = cbearRows;
            });
"""
# Insert listener after `socket.on('disconnect', () => { ... });`
target_str = """            socket.on('disconnect', () => {
                livePill.classList.remove('live');
                livePill.classList.add('off');
                livePill.innerHTML = '<span class="dot"></span> Offline';
            });"""
html = html.replace(target_str, target_str + "\n" + ws_listener)

with open('templates/index.html', 'w') as f:
    f.write(html)
print("Done")
