"""Index report generation: self-contained HTML dashboard + JSON/CSV exports."""
import csv
import html
import io
import json
import time
from collections import Counter
from datetime import datetime, timezone

TYPE_LABELS = {
    "photo": "📷 Photos", "video": "🎬 Videos", "video_note": "⭕ Video messages",
    "animation": "🎞 GIFs/Animations", "document": "📄 Documents", "audio": "🎵 Audio",
    "voice": "🎤 Voice messages", "sticker": "😀 Stickers", "poll": "📊 Polls",
    "location": "📍 Locations", "contact": "👤 Contacts", "link": "🔗 Link posts",
    "text": "💬 Text", "service": "⚙️ Service", "other": "📦 Other",
}


def build_stats(job: dict, rows: list) -> dict:
    by_type = Counter(r["type"] for r in rows)
    links = Counter()
    for r in rows:
        for u in r.get("urls") or []:
            links[u] += 1
    senders = Counter(r["sender"] for r in rows if r.get("sender"))
    files = sorted((r for r in rows if (r.get("size") or 0) > 0), key=lambda r: r["size"], reverse=True)[:25]

    daily = Counter()
    for r in rows:
        if r.get("ts"):
            day = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m")
            daily[day] += 1
    months = sorted(daily.items())
    if len(months) > 24:
        months = months[-24:]

    dates = [r["ts"] for r in rows if r.get("ts")]
    return {
        "total_messages": len(rows),
        "by_type": dict(by_type),
        "total_size_bytes": sum(r.get("size") or 0 for r in rows),
        "unique_links": len(links),
        "top_links": links.most_common(25),
        "top_senders": senders.most_common(15),
        "top_files": [{"msg_id": f["msg_id"], "preview": f["preview"], "size": f["size"],
                       "sender": f.get("sender", "")} for f in files],
        "activity_by_month": months,
        "date_range": [min(dates) if dates else None, max(dates) if dates else None],
        "generated_at": time.time(),
    }


def to_json(job: dict, rows: list, stats: dict) -> str:
    return json.dumps({
        "job": {k: job.get(k) for k in ("id", "kind", "status", "total", "copied", "skipped", "failed")},
        "source": job.get("source"),
        "stats": stats,
        "messages": rows,
    }, indent=2, ensure_ascii=False, default=str)


def to_csv(rows: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["message_id", "date_utc", "type", "sender", "size_bytes", "urls", "preview"])
    for r in rows:
        ts = r.get("ts")
        date = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else ""
        w.writerow([r["msg_id"], date, r["type"], r.get("sender", ""), r.get("size", 0),
                    " | ".join(r.get("urls") or []), r.get("preview", "")])
    return buf.getvalue()


# ------------------------------------------------------------------ HTML

_CSS = """
:root{--bg:#0b1220;--card:#121a2e;--card2:#0f1626;--border:#22304f;--text:#e8eef9;--muted:#8ea0c0;
--accent:#2aabee;--accent2:#229ed9;--ok:#3ecf8e;--warn:#f5b942;--bad:#ef6a6a}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
background:var(--bg);color:var(--text)}
.wrap{max-width:1100px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:34px 0 12px;color:var(--accent)}
.sub{color:var(--muted);font-size:13px;margin-bottom:24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px}
.card .n{font-size:24px;font-weight:700}.card .l{color:var(--muted);font-size:12px;margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:13px;background:var(--card);border-radius:12px;overflow:hidden}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--border);vertical-align:top}
th{background:var(--card2);color:var(--muted);font-weight:600;position:sticky;top:0}
tr:last-child td{border-bottom:none}
.bar-row{display:flex;align-items:center;gap:10px;margin:6px 0;font-size:13px}
.bar-row .lbl{width:180px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar-row .bar{height:14px;border-radius:7px;background:linear-gradient(90deg,var(--accent),var(--accent2));min-width:2px}
.bar-row .cnt{width:70px;text-align:right;color:var(--text)}
.pill{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;background:var(--card2);
border:1px solid var(--border);color:var(--muted)}
.scroll{max-height:520px;overflow:auto;border:1px solid var(--border);border-radius:12px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.muted{color:var(--muted)}.tdl{max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
"""


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _fmt_size(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def to_html(job: dict, rows: list, stats: dict, base_url: str = "") -> str:
    source = job.get("source") or {}
    src_label = source.get("title") or source.get("id") or "?"
    created = datetime.fromtimestamp(stats["generated_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dr = stats["date_range"]
    span = ""
    if dr[0] and dr[1]:
        f1 = datetime.fromtimestamp(dr[0], tz=timezone.utc).strftime("%d %b %Y")
        f2 = datetime.fromtimestamp(dr[1], tz=timezone.utc).strftime("%d %b %Y")
        span = f" · content from {f1} to {f2}"

    type_rows = sorted(stats["by_type"].items(), key=lambda kv: -kv[1])
    max_n = max((n for _, n in type_rows), default=1)
    bars = "".join(
        f'<div class="bar-row"><div class="lbl">{_esc(TYPE_LABELS.get(t, t))}</div>'
        f'<div class="bar" style="width:{max(2, int(n / max_n * 60))}%"></div>'
        f'<div class="cnt">{n}</div></div>'
        for t, n in type_rows
    )

    act_max = max((n for _, n in stats["activity_by_month"]), default=1)
    activity = "".join(
        f'<div class="bar-row"><div class="lbl">{_esc(m)}</div>'
        f'<div class="bar" style="width:{max(2, int(n / act_max * 60))}%"></div>'
        f'<div class="cnt">{n}</div></div>'
        for m, n in stats["activity_by_month"]
    ) or '<p class="muted">No dated messages.</p>'

    links = "".join(
        f'<tr><td class="tdl"><a href="{_esc(u)}" target="_blank" rel="noopener">{_esc(u)}</a></td><td>{n}</td></tr>'
        for u, n in stats["top_links"]
    ) or '<tr><td colspan="2" class="muted">No links found.</td></tr>'

    senders = "".join(
        f'<tr><td>{_esc(s)}</td><td>{n}</td></tr>' for s, n in stats["top_senders"]
    ) or '<tr><td colspan="2" class="muted">Unknown (channel posts).</td></tr>'

    files = "".join(
        f'<tr><td>{r["msg_id"]}</td><td class="tdl">{_esc(r["preview"])}</td>'
        f'<td>{_fmt_size(r["size"])}</td></tr>'
        for r in stats["top_files"]
    ) or '<tr><td colspan="3" class="muted">No files found.</td></tr>'

    MAX_ROWS = 3000
    shown = rows[:MAX_ROWS]
    msgs = "".join(
        f'<tr><td>{r["msg_id"]}</td><td>{datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m-%d") if r.get("ts") else "—"}</td>'
        f'<td><span class="pill">{_esc(r["type"])}</span></td><td class="muted">{_esc(r.get("sender") or "")}</td>'
        f'<td>{_fmt_size(r.get("size") or 0)}</td><td class="tdl">{_esc(r.get("preview") or "")}</td></tr>'
        for r in shown
    )
    more_note = "" if len(rows) <= MAX_ROWS else (
        f'<p class="muted">Showing first {MAX_ROWS} of {len(rows)} messages — full data is in the JSON/CSV exports.</p>'
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Teleporter Index Report — {_esc(src_label)}</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
<h1>📡 Index Report</h1>
<div class="sub">Source: <b>{_esc(src_label)}</b> (<code>{_esc(source.get('id'))}</code>) · generated {created}{span}</div>

<div class="cards">
<div class="card"><div class="n">{stats['total_messages']:,}</div><div class="l">Messages indexed</div></div>
<div class="card"><div class="n">{len(stats['by_type'])}</div><div class="l">Content types found</div></div>
<div class="card"><div class="n">{_fmt_size(stats['total_size_bytes'])}</div><div class="l">Total media size</div></div>
<div class="card"><div class="n">{stats['unique_links']:,}</div><div class="l">Unique links</div></div>
</div>

<h2>Content by type</h2>{bars}

<h2>Activity by month</h2>{activity}

<h2>Top links</h2><div class="scroll"><table><tr><th>Link</th><th>Times</th></tr>{links}</table></div>

<h2>Top contributors</h2><div class="scroll"><table><tr><th>Sender</th><th>Messages</th></tr>{senders}</table></div>

<h2>Largest files</h2><div class="scroll"><table><tr><th>#</th><th>File</th><th>Size</th></tr>{files}</table></div>

<h2>Message index</h2>{more_note}
<div class="scroll"><table>
<tr><th>ID</th><th>Date</th><th>Type</th><th>Sender</th><th>Size</th><th>Preview</th></tr>
{msgs}</table></div>

<p class="muted" style="margin-top:26px">Generated by Teleporter ·
<a href="{base_url}/api/jobs/{_esc(job['id'])}/report.json">Download JSON</a> ·
<a href="{base_url}/api/jobs/{_esc(job['id'])}/report.csv">Download CSV</a></p>
</div></body></html>"""
