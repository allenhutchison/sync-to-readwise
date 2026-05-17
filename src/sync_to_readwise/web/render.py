"""Server-side rendering of the status page from a `SyncState` snapshot.

Pure functions — no I/O, no globals. Given a snapshot dict they return a
complete HTML document, which makes them trivial to unit-test.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

_CSS = """
:root {
  --bg:#16130f; --bg-card:#211b14; --line:#3a3024; --line-soft:#2a2319;
  --ink:#f2e8d6; --ink-mute:#9d917c; --ink-faint:#6a6051;
  --amber:#e8a33d; --green:#8fbd5e; --red:#df6b43;
  --mono:'IBM Plex Mono',ui-monospace,monospace; --disp:'Fraunces',Georgia,serif;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{
  background:var(--bg);color:var(--ink);font-family:var(--mono);font-size:14px;
  line-height:1.5;min-height:100vh;
  background-image:radial-gradient(circle at 50% -10%,rgba(232,163,61,.10),transparent 55%),
    radial-gradient(rgba(255,255,255,.022) 1px,transparent 1px);
  background-size:100% 100%,22px 22px;background-attachment:fixed;
}
a{color:inherit;}
.wrap{max-width:1120px;margin:0 auto;padding:0 28px;}
.topbar{display:flex;align-items:center;justify-content:space-between;
  border-bottom:1px solid var(--line);padding:18px 0;}
.brand{display:flex;align-items:center;gap:12px;}
.mark{width:26px;height:26px;border-radius:50%;position:relative;
  background:conic-gradient(from 220deg,var(--amber),#5c4a2a 60%,var(--amber));
  box-shadow:0 0 0 1px var(--line),0 0 16px rgba(232,163,61,.35);}
.mark::after{content:"";position:absolute;inset:7px;border-radius:50%;background:var(--bg);}
.brand b{font-weight:600;letter-spacing:.14em;font-size:13px;}
.brand span{color:var(--ink-faint);font-size:13px;}
.topbar .meta{display:flex;gap:22px;align-items:center;font-size:12px;color:var(--ink-mute);}
.clock{color:var(--ink);letter-spacing:.06em;}
.pill{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);
  border-radius:100px;padding:6px 13px 6px 10px;font-size:12px;letter-spacing:.04em;}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);}
.dot.amber{background:var(--amber);}
.dot.red{background:var(--red);}
.hero{padding:64px 0 40px;border-bottom:1px solid var(--line-soft);
  display:grid;grid-template-columns:1fr auto;gap:40px;align-items:end;}
.kicker{font-size:12px;letter-spacing:.3em;text-transform:uppercase;
  color:var(--amber);margin-bottom:18px;}
.hero h1{font-family:var(--disp);font-optical-sizing:auto;font-weight:500;
  font-size:clamp(2.4rem,6.5vw,4.8rem);line-height:1;letter-spacing:-.02em;}
.hero h1 em{font-style:italic;color:var(--amber);font-weight:400;}
.hero .sub{color:var(--ink-mute);margin-top:18px;font-size:13px;max-width:46ch;}
.hero .sub b{color:var(--ink);font-weight:500;}
.uptime{text-align:right;white-space:nowrap;border-left:1px solid var(--line-soft);
  padding-left:32px;}
.uptime .n{font-family:var(--disp);font-size:3rem;font-weight:500;
  line-height:1;letter-spacing:-.02em;}
.uptime .l{font-size:11px;letter-spacing:.2em;text-transform:uppercase;
  color:var(--ink-faint);margin-top:8px;}
section{padding:44px 0;border-bottom:1px solid var(--line-soft);}
.sec-head{display:flex;align-items:baseline;gap:14px;margin-bottom:24px;}
.sec-head h2{font-size:12px;letter-spacing:.22em;text-transform:uppercase;
  font-weight:600;}
.sec-head .rule{flex:1;height:1px;background:var(--line-soft);}
.sec-head .count{font-size:12px;color:var(--ink-faint);}
.channels{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px;}
.card{background:var(--bg-card);border:1px solid var(--line);border-radius:4px;
  padding:22px;position:relative;overflow:hidden;}
.card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;
  background:var(--green);}
.card.warn::before{background:var(--amber);}
.card.fail::before{background:var(--red);}
.card .row1{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px;}
.card .name{font-size:16px;font-weight:600;letter-spacing:.02em;}
.card .state{font-size:11px;letter-spacing:.12em;text-transform:uppercase;
  display:flex;align-items:center;gap:7px;color:var(--ink-mute);}
.card .schedule{font-size:12px;color:var(--ink-faint);margin-bottom:18px;}
.spark{display:flex;align-items:flex-end;gap:3px;height:46px;padding:8px 0;
  margin-bottom:16px;border-bottom:1px dashed var(--line-soft);}
.spark i{flex:1;background:var(--line);border-radius:1px;min-height:2px;
  transform-origin:bottom;animation:grow .7s cubic-bezier(.2,.8,.2,1) backwards;}
.spark i.hit{background:var(--amber);box-shadow:0 0 8px rgba(232,163,61,.4);}
@keyframes grow{from{transform:scaleY(0);}}
.counters{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;}
.counter .v{font-family:var(--disp);font-size:1.8rem;font-weight:500;
  line-height:1;letter-spacing:-.02em;}
.counter .k{font-size:10px;letter-spacing:.16em;text-transform:uppercase;
  color:var(--ink-faint);margin-top:6px;}
.counter.alert .v{color:var(--red);}
.counter.gain .v{color:var(--green);}
.card .foot{margin-top:18px;padding-top:14px;border-top:1px solid var(--line-soft);
  display:flex;justify-content:space-between;font-size:12px;color:var(--ink-mute);}
.card .foot b{color:var(--ink);font-weight:500;}
.card .err{margin-top:14px;font-size:12px;color:var(--red);
  background:rgba(223,107,67,.08);border:1px solid rgba(223,107,67,.3);
  border-radius:3px;padding:10px 12px;}
.cred{display:flex;align-items:center;gap:20px;background:var(--bg-card);
  border:1px solid var(--line);border-radius:4px;padding:18px 22px;}
.cred .icon{width:38px;height:38px;flex:none;border-radius:4px;
  border:1px solid var(--line);display:grid;place-items:center;font-size:17px;}
.cred .body{flex:1;min-width:0;}
.cred .body .t{font-weight:600;font-size:14px;}
.cred .body .d{font-size:12px;color:var(--ink-mute);margin-top:2px;}
.cred .verdict{font-size:11px;letter-spacing:.12em;text-transform:uppercase;
  display:flex;align-items:center;gap:7px;white-space:nowrap;color:var(--ink-mute);}
.btn{font-family:var(--mono);font-size:12px;font-weight:500;letter-spacing:.06em;
  text-transform:uppercase;border:1px solid var(--amber);color:var(--bg);
  background:var(--amber);border-radius:3px;padding:9px 16px;cursor:pointer;
  white-space:nowrap;text-decoration:none;display:inline-block;
  transition:transform .12s ease,box-shadow .12s ease;}
.btn:hover{transform:translateY(-1px);box-shadow:0 4px 14px rgba(232,163,61,.3);}
.btn.ghost{background:transparent;color:var(--ink-mute);border-color:var(--line);}
.log{display:flex;flex-direction:column;gap:1px;background:var(--line-soft);
  border:1px solid var(--line);border-radius:4px;overflow:hidden;}
.log .ev{display:grid;grid-template-columns:150px 110px 1fr;gap:16px;
  padding:11px 18px;background:var(--bg-card);font-size:12.5px;align-items:baseline;}
.log .ev .ts{color:var(--ink-faint);}
.log .ev .tag{font-size:10px;letter-spacing:.1em;text-transform:uppercase;
  align-self:center;width:max-content;padding:3px 8px;border-radius:3px;
  border:1px solid var(--line);color:var(--ink-mute);}
.log .ev .tag.created{color:var(--green);border-color:rgba(143,189,94,.4);}
.log .ev .tag.error{color:var(--red);border-color:rgba(223,107,67,.5);}
.log .ev .msg{color:var(--ink);overflow-wrap:anywhere;}
.log .ev .msg a{color:var(--amber);text-decoration:none;
  border-bottom:1px solid rgba(232,163,61,.3);}
.log .ev .msg .src{color:var(--ink-faint);}
.empty{color:var(--ink-faint);font-size:13px;padding:8px 0;}
footer{padding:30px 0 50px;color:var(--ink-faint);font-size:11px;
  display:flex;justify-content:space-between;letter-spacing:.04em;}
.notice{max-width:560px;margin:14vh auto 0;text-align:center;padding:0 28px;}
.notice .mark{margin:0 auto 28px;}
.notice h1{font-family:var(--disp);font-weight:500;font-size:2.4rem;
  letter-spacing:-.02em;margin-bottom:14px;}
.notice p{color:var(--ink-mute);margin-bottom:26px;}
@media(max-width:740px){
  .hero{grid-template-columns:1fr;}
  .uptime{border-left:0;padding-left:0;text-align:left;}
  .log .ev{grid-template-columns:1fr;gap:4px;}
  .cred{flex-wrap:wrap;}
}
"""

_FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?'
    "family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;1,9..144,400&"
    'family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">'
)

_MARK = '<div class="mark"></div>'

# Per-source display metadata. Sources not listed fall back to a generic glyph.
_SOURCE_GLYPH = {"youtube": "▶", "github_stars": "★"}


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _delta_str(seconds: float) -> str:
    seconds = int(abs(seconds))
    if seconds < 60:
        return f"{seconds} sec"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr"
    days = hours // 24
    return f"{days} day" + ("s" if days != 1 else "")


def _rel_past(value: str | None, now: datetime) -> str:
    dt = _parse(value)
    if dt is None:
        return "never"
    return f"{_delta_str((now - dt).total_seconds())} ago"


def _rel_future(value: str | None, now: datetime) -> str:
    dt = _parse(value)
    if dt is None:
        return "—"
    seconds = (dt - now).total_seconds()
    if seconds <= 0:
        return "due now"
    return f"in {_delta_str(seconds)}"


def _uptime(value: str | None, now: datetime) -> str:
    dt = _parse(value)
    if dt is None:
        return "—"
    seconds = int((now - dt).total_seconds())
    days, rem = divmod(max(seconds, 0), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _document(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_esc(title)}</title>{_FONTS}"
        f"<style>{_CSS}</style></head><body>{body}</body></html>"
    )


def _sparkline(history: list[int]) -> str:
    """Render the per-sync created-count history as a row of bars."""
    if not history:
        bars = '<i style="height:6%"></i>' * 14
        return f'<div class="spark">{bars}</div>'
    peak = max(history) or 1
    bars = []
    for value in history:
        height = 8 + (value / peak) * 92
        cls = " class=\"hit\"" if value > 0 else ""
        bars.append(f'<i{cls} style="height:{height:.0f}%"></i>')
    return f'<div class="spark">{"".join(bars)}</div>'


def _counter(label: str, value: object, kind: str = "") -> str:
    cls = f" {kind}" if kind else ""
    shown = "—" if value is None else _esc(value)
    return (
        f'<div class="counter{cls}"><div class="v">{shown}</div>'
        f'<div class="k">{_esc(label)}</div></div>'
    )


def _channel_card(name: str, src: dict, now: datetime) -> str:
    status = src.get("last_status")
    if status == "error":
        card_cls, dot, state = "fail", "red", "failing"
    elif status == "ok":
        card_cls, dot, state = "", "", "healthy"
    else:
        card_cls, dot, state = "warn", "amber", "awaiting first sync"

    glyph = _SOURCE_GLYPH.get(name, "◆")
    interval = src.get("interval_minutes")
    schedule = f"every {interval} min" if interval else "schedule unknown"

    result = src.get("last_result") or {}
    counters = (
        _counter("seen", result.get("seen"))
        + _counter("created", result.get("created"), "gain" if result.get("created") else "")
        + _counter("skipped", result.get("skipped"))
        + _counter("errors", result.get("errors"), "alert" if result.get("errors") else "")
    )

    err_html = ""
    if status == "error" and src.get("last_error"):
        err_html = f'<div class="err">{_esc(src["last_error"])}</div>'

    return (
        f'<div class="card {card_cls}">'
        f'<div class="row1"><div class="name">{_esc(glyph)} {_esc(name)}</div>'
        f'<div class="state"><span class="dot {dot}"></span>{_esc(state)}</div></div>'
        f'<div class="schedule">{_esc(schedule)}</div>'
        f"{_sparkline(src.get('history') or [])}"
        f'<div class="counters">{counters}</div>'
        f'<div class="foot"><span>last run <b>{_rel_past(src.get("last_run_at"), now)}</b>'
        f"</span><span>next <b>{_rel_future(src.get('next_run_at'), now)}</b></span></div>"
        f"{err_html}</div>"
    )


def _credentials(sources: dict, now: datetime) -> str:
    """Credential panel — currently just the YouTube OAuth re-auth entry point."""
    yt = sources.get("youtube")
    if yt is None:
        return ""
    if yt.get("auth_failed"):
        dot, verdict = "red", "re-authorization required"
        detail = "The stored OAuth token expired or was revoked."
    elif yt.get("last_status") == "ok":
        dot, verdict = "", "connected"
        detail = f"Last successful sync {_rel_past(yt.get('last_success_at'), now)}."
    else:
        dot, verdict = "amber", "unverified"
        detail = "No successful sync yet — re-authorize if syncs keep failing."
    return (
        '<section><div class="sec-head"><h2>Credentials</h2>'
        '<div class="rule"></div><span class="count">youtube oauth</span></div>'
        '<div class="cred"><div class="icon">▶</div>'
        f'<div class="body"><div class="t">YouTube OAuth token</div>'
        f'<div class="d">{_esc(detail)}</div></div>'
        f'<div class="verdict"><span class="dot {dot}"></span>{_esc(verdict)}</div>'
        '<a class="btn" href="/auth/youtube">Re-authorize</a></div></section>'
    )


def _event_row(event: dict) -> str:
    kind = event.get("kind", "")
    tag = {"ok": "sync ok", "created": "created", "error": "error"}.get(kind, kind)
    message = event.get("message", "")
    url = event.get("url")
    msg_html = f'<a href="{_esc(url)}">{_esc(message)}</a>' if url else _esc(message)
    src = event.get("source")
    src_html = f' <span class="src">· {_esc(src)}</span>' if src else ""
    return (
        f'<div class="ev"><span class="ts">{_esc(event.get("at", ""))}</span>'
        f'<span class="tag {_esc(kind)}">{_esc(tag)}</span>'
        f'<span class="msg">{msg_html}{src_html}</span></div>'
    )


def _hero(sources: dict, now: datetime, started_at: str | None) -> tuple[str, str]:
    """Return (pill_html, hero_html) describing overall daemon health."""
    failing = sorted(n for n, s in sources.items() if s.get("last_status") == "error")
    ran = [s for s in sources.values() if s.get("last_status") is not None]

    if failing:
        pill_dot, pill_text = "red", f"{len(failing)} failing"
        kicker = "Action required"
        if len(failing) == 1:
            headline = "A <em>channel</em><br>needs attention."
        else:
            headline = f"<em>{len(failing)} channels</em><br>need attention."
        sub = f"Failing: <b>{_esc(', '.join(failing))}</b>. See the channel detail below."
    elif not ran:
        pill_dot, pill_text = "amber", "starting up"
        kicker = "Daemon report"
        headline = "Waiting for<br>the <em>first sync</em>."
        sub = "The daemon is up. Channels report in as their schedules fire."
    else:
        pill_dot, pill_text = "", "operational"
        kicker = "Daemon report"
        headline = "All channels<br><em>nominal</em>."
        sub = "Every channel synced on schedule. Nothing needs attention."

    pill = f'<span class="pill"><span class="dot {pill_dot}"></span>{_esc(pill_text)}</span>'
    hero = (
        '<div class="hero"><div>'
        f'<div class="kicker">{_esc(kicker)}</div><h1>{headline}</h1>'
        f'<p class="sub">{sub}</p></div>'
        f'<div class="uptime"><div class="n">{_uptime(started_at, now)}</div>'
        '<div class="l">Uptime</div></div></div>'
    )
    return pill, hero


_CLOCK_JS = (
    "<script>function t(){var d=new Date(),p=function(n){return"
    "(''+n).padStart(2,'0')};document.getElementById('clk').textContent="
    "p(d.getUTCHours())+':'+p(d.getUTCMinutes())+':'+p(d.getUTCSeconds())+"
    "' UTC';}t();setInterval(t,1000);</script>"
)


def render_status_page(snapshot: dict, *, now: datetime | None = None) -> str:
    """Render the full status page HTML from a `SyncState.snapshot()` dict."""
    now = now or datetime.now(UTC)
    sources = snapshot.get("sources", {})
    events = snapshot.get("recent_events", [])

    pill, hero = _hero(sources, now, snapshot.get("daemon_started_at"))

    if sources:
        cards = "".join(
            _channel_card(name, sources[name], now) for name in sorted(sources)
        )
        channels = f'<div class="channels">{cards}</div>'
    else:
        channels = '<p class="empty">No sources are enabled.</p>'

    if events:
        log = '<div class="log">' + "".join(_event_row(e) for e in events) + "</div>"
    else:
        log = '<p class="empty">No activity recorded yet.</p>'

    body = (
        '<div class="topbar wrap"><div class="brand">'
        f"{_MARK}<b>SYNC·TO·READWISE</b><span>/ status</span></div>"
        '<div class="meta"><span class="clock" id="clk">--:--:-- UTC</span>'
        f"{pill}</div></div>"
        f'<div class="wrap">{hero}'
        '<section><div class="sec-head"><h2>Channels</h2><div class="rule"></div>'
        f'<span class="count">{len(sources)} enabled</span></div>{channels}</section>'
        f"{_credentials(sources, now)}"
        '<section><div class="sec-head"><h2>Recent activity</h2>'
        f'<div class="rule"></div><span class="count">{len(events)} events</span></div>'
        f"{log}</section>"
        '<footer><span>sync-to-readwise status</span>'
        "<span>auto-refreshes every 30s</span></footer></div>"
        f"{_CLOCK_JS}"
    )
    # Cheap live-ish refresh without client JS polling.
    body = '<meta http-equiv="refresh" content="30">' + body
    return _document("sync-to-readwise · status", body)


def render_message(title: str, detail: str, *, link: tuple[str, str] | None = None) -> str:
    """Render a standalone notice page (auth success/failure, errors)."""
    link_html = ""
    if link:
        label, href = link
        link_html = f'<a class="btn" href="{_esc(href)}">{_esc(label)}</a>'
    body = (
        f'<div class="notice">{_MARK}<h1>{_esc(title)}</h1>'
        f"<p>{_esc(detail)}</p>{link_html}</div>"
    )
    return _document(f"{title} · sync-to-readwise", body)
