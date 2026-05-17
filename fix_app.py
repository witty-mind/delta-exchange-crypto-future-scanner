import re

with open('app.py', 'r') as f:
    html = f.read()

# Fix syntax error at def init_db() \n _bootstrap_tickers_once():
html = html.replace("def init_db()\\n    _bootstrap_tickers_once():", "def _bootstrap_tickers_once():")

# Fix futs()
html = html.replace("            init_db()\\n    _bootstrap_tickers_once()", "            _bootstrap_tickers_once()")

with open('app.py', 'w') as f:
    f.write(html)
