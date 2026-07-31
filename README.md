# majidershadi.github.io static site update

This directory is a complete drop-in replacement/addition for the existing static GitHub Pages repository.

It deliberately remains a hand-written static site. No Jekyll, npm, framework, or GitHub Actions build is required.

Run locally:

```bash
python3 scripts/check_site.py
python3 -m http.server 8000
```

Then open `http://127.0.0.1:8000/`.
