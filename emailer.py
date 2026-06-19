"""
Transactional email via SendGrid (stdlib HTTP — no extra dependency).

Graceful: if SENDGRID_API_KEY is unset, send_* functions return False without
raising, so lead capture still works (the email just isn't sent).

Env:
  SENDGRID_API_KEY  — SendGrid API key
  EMAIL_FROM        — verified sender (default grader@shazamme.com)
  EMAIL_FROM_NAME   — sender name (default 'Shazamme Grader')
  PUBLIC_BASE_URL   — base for links in emails
"""

import os
import json
import urllib.request
import urllib.error

API_URL = 'https://api.sendgrid.com/v3/mail/send'


def _cfg():
    return {
        'key': os.environ.get('SENDGRID_API_KEY', ''),
        'from': os.environ.get('EMAIL_FROM', 'grader@shazamme.com'),
        'from_name': os.environ.get('EMAIL_FROM_NAME', 'Shazamme Grader'),
        'base': os.environ.get('PUBLIC_BASE_URL',
                               'https://career-site-grader-production.up.railway.app'),
    }


def enabled() -> bool:
    return bool(os.environ.get('SENDGRID_API_KEY', ''))


def _send(to: str, subject: str, html: str) -> bool:
    cfg = _cfg()
    if not cfg['key']:
        return False
    payload = {
        'personalizations': [{'to': [{'email': to}]}],
        'from': {'email': cfg['from'], 'name': cfg['from_name']},
        'subject': subject,
        'content': [{'type': 'text/html', 'value': html}],
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(payload).encode('utf-8'),
        headers={'Authorization': f"Bearer {cfg['key']}", 'Content-Type': 'application/json'},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _grade_colour(score):
    if score >= 85: return '#10b981'
    if score >= 65: return '#22d3ee'
    if score >= 45: return '#f59e0b'
    return '#f43f5e'


def _shell(inner: str) -> str:
    return (
        '<div style="font-family:Inter,Arial,sans-serif;background:#0a0a12;color:#f1f1f6;'
        'padding:32px;border-radius:16px;max-width:600px;margin:auto">' + inner +
        '<p style="color:#7a7a8c;font-size:12px;margin-top:28px">Shazamme Career Site Grader · '
        '<a href="https://www.shazamme.com" style="color:#22d3ee">shazamme.com</a></p></div>'
    )


def send_report_email(to: str, url: str, mode: str, overall: int, grade: str,
                      report_link: str, top_fixes=None) -> bool:
    colour = _grade_colour(overall)
    fixes_html = ''
    if top_fixes:
        items = ''.join(
            f'<li style="margin-bottom:10px"><strong>{f.get("title","")}</strong> '
            f'<span style="color:#b4b4c4">— {f.get("how_to_fix","")}</span></li>'
            for f in top_fixes[:3])
        fixes_html = (f'<p style="font-weight:700;margin:24px 0 8px">Top 3 things to fix first</p>'
                      f'<ul style="padding-left:18px;color:#f1f1f6;line-height:1.5">{items}</ul>')
    inner = (
        f'<p style="font-size:13px;color:#b4b4c4;margin:0 0 6px">Your grade for</p>'
        f'<p style="font-size:20px;font-weight:700;margin:0 0 18px">{url}</p>'
        f'<div style="display:inline-block;background:{colour}22;border:2px solid {colour}55;'
        f'border-radius:14px;padding:16px 26px;text-align:center">'
        f'<div style="font-size:40px;font-weight:800;color:{colour};line-height:1">{overall}</div>'
        f'<div style="font-size:13px;color:#b4b4c4">out of 100 · grade {grade}</div></div>'
        f'{fixes_html}'
        f'<p style="margin:26px 0"><a href="{report_link}" '
        f'style="background:linear-gradient(120deg,#8b5cf6,#6366f1);color:#fff;text-decoration:none;'
        f'padding:13px 22px;border-radius:10px;font-weight:600">View your full report</a></p>'
    )
    return _send(to, f'Your career-site grade: {overall}/100 ({grade})', _shell(inner))


def send_monitor_digest(to: str, url: str, overall: int, grade: str,
                        previous: int, report_link: str) -> bool:
    delta = overall - previous if previous is not None else None
    if delta is None:
        trend = ''
    elif delta > 0:
        trend = f'<span style="color:#10b981">▲ +{delta} since last month</span>'
    elif delta < 0:
        trend = f'<span style="color:#f43f5e">▼ {delta} since last month</span>'
    else:
        trend = '<span style="color:#7a7a8c">no change since last month</span>'
    colour = _grade_colour(overall)
    inner = (
        f'<p style="font-size:13px;color:#b4b4c4;margin:0 0 6px">Monthly re-score for</p>'
        f'<p style="font-size:20px;font-weight:700;margin:0 0 18px">{url}</p>'
        f'<div style="font-size:40px;font-weight:800;color:{colour}">{overall}<span '
        f'style="font-size:16px;color:#b4b4c4"> /100 · {grade}</span></div>'
        f'<p style="margin:6px 0 0">{trend}</p>'
        f'<p style="margin:24px 0"><a href="{report_link}" '
        f'style="background:linear-gradient(120deg,#8b5cf6,#6366f1);color:#fff;text-decoration:none;'
        f'padding:13px 22px;border-radius:10px;font-weight:600">See what changed</a></p>'
    )
    return _send(to, f'{url} is now {overall}/100', _shell(inner))
