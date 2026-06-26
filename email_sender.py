import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from markupsafe import escape


def send_link_invite_email(gmail_user, gmail_app_password, recipient, accept_url, inviter_email):
    subject = f"{inviter_email} wants to link accounts with you"
    html = f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#333;max-width:560px;">
      <h2 style="color:#1a3c8a;">Account link invitation</h2>
      <p><strong>{escape(inviter_email)}</strong> wants to link accounts with you on Dimes.</p>
      <p>If you accept, the two of you share <strong>everything</strong> — all cards, benefits, redemptions, and offers become one shared wallet, and either of you can add, edit, or redeem any of it. You each keep your own login, your own reminder email address, and your own on/off switch for reminder emails.</p>
      <p style="margin:24px 0;">
        <a href="{accept_url}" style="background:#1a3c8a;color:#fff;text-decoration:none;
            padding:12px 22px;border-radius:6px;font-weight:600;display:inline-block;">
          Review the invitation
        </a>
      </p>
      <p style="font-size:13px;color:#666;">
        If the button doesn't work, paste this URL into your browser:<br>
        <a href="{accept_url}">{accept_url}</a>
      </p>
      <p style="font-size:12px;color:#999;border-top:1px solid #eee;padding-top:12px;margin-top:24px;">
        The link expires in 24 hours. If you didn't expect this, you can safely ignore the email — nothing happens until you click Accept.
      </p>
    </body></html>
    """

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = gmail_user
    msg['To']      = recipient
    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, recipient, msg.as_string())


def send_reset_email(gmail_user, gmail_app_password, recipient, reset_url):
    subject = "Reset your Dimes password"
    html = f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#333;max-width:560px;">
      <h2 style="color:#1a3c8a;">Reset your password</h2>
      <p>Someone (hopefully you) requested a password reset for <strong>{escape(recipient)}</strong>.</p>
      <p>Click the button below to set a new password. The link expires in 24 hours.</p>
      <p style="margin:24px 0;">
        <a href="{reset_url}" style="background:#1a3c8a;color:#fff;text-decoration:none;
            padding:12px 22px;border-radius:6px;font-weight:600;display:inline-block;">
          Reset password
        </a>
      </p>
      <p style="font-size:13px;color:#666;">
        If the button doesn't work, paste this URL into your browser:<br>
        <a href="{reset_url}">{reset_url}</a>
      </p>
      <p style="font-size:12px;color:#999;border-top:1px solid #eee;padding-top:12px;margin-top:24px;">
        If you didn't request this, you can safely ignore this email — your password won't change.
      </p>
    </body></html>
    """

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = gmail_user
    msg['To']      = recipient
    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, recipient, msg.as_string())


def send_invite_email(gmail_user, gmail_app_password, recipient, accept_url, inviter_email):
    subject = f"You've been invited to Dimes"
    html = f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#333;max-width:560px;">
      <h2 style="color:#1a3c8a;">You're invited to Dimes</h2>
      <p><strong>{escape(inviter_email)}</strong> has invited you to track your credit card benefits and redemptions.</p>
      <p>Click the button below to set your password and get started. This link is good for 7 days.</p>
      <p style="margin:24px 0;">
        <a href="{accept_url}" style="background:#1a3c8a;color:#fff;text-decoration:none;
            padding:12px 22px;border-radius:6px;font-weight:600;display:inline-block;">
          Set up my account
        </a>
      </p>
      <p style="font-size:13px;color:#666;">
        If the button doesn't work, paste this URL into your browser:<br>
        <a href="{accept_url}">{accept_url}</a>
      </p>
      <p style="font-size:12px;color:#999;border-top:1px solid #eee;padding-top:12px;margin-top:24px;">
        If you didn't expect this, you can safely ignore the email.
      </p>
    </body></html>
    """

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = gmail_user
    msg['To']      = recipient
    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, recipient, msg.as_string())


def _offer_blocks_html(offers, font):
    """Render the gift-card/coupon/promotion cards shown in reminder emails."""
    blocks = ""
    for o in offers:
        name = escape(o['name'])
        dl  = o.get('days_left')
        exp = o.get('expiration')
        if exp and dl is not None:
            urg = "#dc2626" if dl <= 3 else ("#ea7317" if dl <= 7 else "#16a34a")
            pill_txt = "Expires today" if dl <= 0 else f"{dl} day{'s' if dl != 1 else ''} left"
            day_pill = (f'<span style="background:{urg};color:#ffffff;font:700 11px {font};'
                        f'padding:4px 11px;border-radius:999px;white-space:nowrap;">{pill_txt}</span>')
        else:
            day_pill = ''
        meta_bits = []
        if o.get('detail'):
            meta_bits.append(escape(o['detail']))
        meta_bits.append(f"expires {escape(exp)}" if exp else "no expiration")
        meta = ' &middot; '.join(meta_bits)
        blocks += f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="border:1px solid #e6eaf2;border-radius:10px;background:#ffffff;margin:0 0 14px;">
          <tr><td style="padding:15px 16px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td style="font:600 15px {font};color:#1a2233;">{name}</td>
                <td align="right" style="white-space:nowrap;padding-left:10px;">{day_pill}</td>
              </tr>
            </table>
            <div style="font:400 12px {font};color:#8a93a6;margin-top:5px;">{meta}</div>
          </td></tr>
        </table>"""
    return blocks


def _unsubscribe_html(unsubscribe_url, font):
    """Footer line letting the recipient turn off all recurring email. Shared by
    the reminder and subscription-digest templates so the wording stays in sync."""
    if not unsubscribe_url:
        return ''
    return (f'<div style="margin-top:8px;">'
            f'Don\'t want these? '
            f'<a href="{unsubscribe_url}" style="color:#1a3c8a;text-decoration:underline;">'
            f'Unsubscribe from all Dimes emails</a>.</div>')


def send_reminder_email(gmail_user, gmail_app_password, recipient, benefits_due,
                        offers=None, unsubscribe_url=None):
    """
    benefits_due: list of dicts with keys:
      card_name, benefit_name, credit_amount,
      amount_used, period_end, days_left
    offers: optional list of dicts (gift cards / coupons / promotions) with keys:
      name, detail, expiration, days_left. When benefits_due is present
      these ride along as an awareness footer; when it's empty they are the email.
    unsubscribe_url: optional signed link that turns off all recurring email for
      the recipient; rendered as an unsubscribe line in the footer when present.
    """
    offers = offers or []
    if not benefits_due and not offers:
        return

    n   = len(benefits_due)
    n_o = len(offers)
    if benefits_due:
        subject = f"{n} benefit{'s' if n != 1 else ''} need attention before the deadline"
    else:
        subject = f"{n_o} offer{'s' if n_o != 1 else ''} to use before they expire"
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

    blocks = ""
    for b in benefits_due:
        card_label    = escape(b['card_name'])
        benefit_label = escape(b['benefit_name'])
        days_left     = b['days_left']

        urg = "#dc2626" if days_left <= 3 else ("#ea7317" if days_left <= 7 else "#16a34a")
        pill = "Due today" if days_left <= 0 else f"{days_left} day{'s' if days_left != 1 else ''} left"

        if b['credit_amount']:
            used      = b['amount_used'] or 0
            remaining = max(0.0, b['credit_amount'] - used)
            pct       = min(100, max(0, int(used / b['credit_amount'] * 100)))
            usage_str = f"${remaining:,.0f} of ${b['credit_amount']:,.0f} left"
            bar_color = "#ea7317" if pct < 60 else "#16a34a"
            # Filled portion (omitted at 0%), sitting on a track that's always shown.
            fill = (f'<table role="presentation" width="{pct}%" cellpadding="0" cellspacing="0" border="0"><tr>'
                    f'<td height="6" style="height:6px;background:{bar_color};border-radius:6px;font-size:0;line-height:6px;">&nbsp;</td>'
                    f'</tr></table>') if pct > 0 else '&nbsp;'
            bar_html = (
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                'style="margin-top:5px;background:#eef1f6;border-radius:6px;"><tr>'
                f'<td height="6" style="height:6px;font-size:0;line-height:6px;">{fill}</td>'
                '</tr></table>'
            )
        else:
            usage_str = "Not yet used this period"
            bar_html  = ''

        button = ''
        if b.get('redeem_url'):
            button = (
                f'<div style="margin-top:14px;">'
                f'<a href="{b["redeem_url"]}" style="background:#1a3c8a;color:#ffffff;text-decoration:none;'
                f'font:600 14px {font};padding:10px 18px;border-radius:8px;display:inline-block;">'
                f'&#10003;&nbsp; Mark redeemed</a></div>'
            )

        blocks += f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
               style="border:1px solid #e6eaf2;border-radius:10px;background:#ffffff;margin:0 0 14px;">
          <tr><td style="padding:15px 16px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td style="font:600 15px {font};color:#1a2233;">{benefit_label}</td>
                <td align="right" style="white-space:nowrap;padding-left:10px;">
                  <span style="background:{urg};color:#ffffff;font:700 11px {font};padding:4px 11px;border-radius:999px;white-space:nowrap;">{pill}</span>
                </td>
              </tr>
            </table>
            <div style="font:400 12px {font};color:#8a93a6;margin-top:3px;">{card_label} &middot; ends {b['period_end']}</div>
            <div style="font:400 12px {font};color:#5b6472;margin-top:12px;">{usage_str}</div>
            {bar_html}
            {button}
          </td></tr>
        </table>"""

    offer_blocks = _offer_blocks_html(offers, font)

    if benefits_due:
        preheader = f"{n} benefit{'s' if n != 1 else ''} with unused credit and a deadline coming up."
        heading   = f"{n} benefit{'s' if n != 1 else ''} need attention"
        intro     = "These have unused credit with a deadline coming up. Tap <b>Mark redeemed</b> to log one in a single step."
        footer_note = "You can change a benefit's reminder schedule from its page in your wallet."
    else:
        preheader = f"{n_o} offer{'s' if n_o != 1 else ''} you haven't used yet."
        heading   = f"{n_o} offer{'s' if n_o != 1 else ''} to use"
        intro     = "These gift cards, coupons and promotions are still unredeemed. Open Dimes to mark them used once you do."
        footer_note = "You can change an offer's reminder schedule, or mark it used, from the Offers page in your wallet."

    benefits_row = f'<tr><td style="padding:16px 24px 4px;">{blocks}</td></tr>' if benefits_due else ''

    if offers:
        offers_heading = (
            f'<div style="font:700 14px {font};color:#1a2233;margin:4px 0 2px;">Gift cards &amp; offers</div>'
            f'<div style="font:400 12px {font};color:#5b6472;margin-bottom:12px;">'
            f'Awareness reminder — mark these used in your wallet once redeemed.</div>'
        ) if benefits_due else ''
        offers_row = f'<tr><td style="padding:8px 24px 4px;">{offers_heading}{offer_blocks}</td></tr>'
    else:
        offers_row = ''

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#eef1f6;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:#eef1f6;">{preheader}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#eef1f6;">
    <tr><td align="center" style="padding:24px 12px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"
             style="width:600px;max-width:100%;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #e1e6f0;">
        <tr><td style="background:#1a3c8a;padding:20px 24px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
            <td style="font:700 18px {font};color:#ffffff;">&#129689;&nbsp; Dimes</td>
            <td align="right" style="font:600 12px {font};color:#aac4ff;letter-spacing:.04em;text-transform:uppercase;">Reminder</td>
          </tr></table>
        </td></tr>
        <tr><td style="padding:24px 24px 4px;">
          <div style="font:700 19px {font};color:#1a2233;">{heading}</div>
          <div style="font:400 14px {font};color:#5b6472;margin-top:6px;line-height:1.5;">
            {intro}
          </div>
        </td></tr>
        {benefits_row}
        {offers_row}
        <tr><td style="padding:4px 24px 26px;">
          <div style="border-top:1px solid #eef1f6;padding-top:16px;font:400 12px {font};color:#98a2b3;line-height:1.6;">
            Sent by <a href="https://dimes.trentschroeder.com" style="color:#1a3c8a;text-decoration:none;">Dimes</a>.
            {footer_note}
            {_unsubscribe_html(unsubscribe_url, font)}
          </div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = gmail_user
    msg['To'] = recipient
    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, recipient, msg.as_string())


def send_card_request_email(gmail_user, gmail_app_password, recipients,
                            requester, card_name, notes, manage_url):
    """Notify admins that a user requested a card not yet in the catalog."""
    if not recipients:
        return
    subject = f"Card request: {card_name}"
    notes_html = (f'<p style="margin:4px 0 0;color:#555;">“{escape(notes)}”</p>'
                  if notes else '')
    html = f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#333;max-width:560px;">
      <h2 style="color:#1a3c8a;">New card request</h2>
      <p><strong>{escape(requester)}</strong> asked for a card that isn't in the catalog yet:</p>
      <p style="font-size:1.1rem;font-weight:600;margin:8px 0;">{escape(card_name)}</p>
      {notes_html}
      <p style="margin:24px 0;">
        <a href="{manage_url}" style="background:#1a3c8a;color:#fff;text-decoration:none;
            padding:12px 22px;border-radius:6px;font-weight:600;display:inline-block;">
          Review in Card Templates
        </a>
      </p>
    </body></html>
    """
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = gmail_user
    msg['To']      = ', '.join(recipients)
    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, recipients, msg.as_string())


def send_subscription_digest_email(gmail_user, gmail_app_password, recipient, groups,
                                   monthly_total, unsubscribe_url=None):
    """Monthly awareness digest: every active subscription and what it costs per
    month, grouped into category sections to match the Subscriptions page.
    `groups` is an ordered list of dicts with keys: name, subtotal, subs — where
    each sub is a dict with keys name, amount, card_label (or None).
    unsubscribe_url: optional signed link to turn off all recurring email.
    No-op when there are no active subscriptions."""
    if not groups:
        return

    n = sum(len(g['subs']) for g in groups)
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
    yearly = monthly_total * 12
    subject = f"Your subscriptions: ${monthly_total:,.2f}/mo across {n} active"

    sections = ""
    for g in groups:
        sections += f"""
        <tr>
          <td colspan="2" style="padding:18px 0 4px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
              <td style="font:700 12px {font};color:#1a3c8a;letter-spacing:.05em;text-transform:uppercase;">{escape(g['name'])}</td>
              <td align="right" style="font:600 12px {font};color:#8a93a6;white-space:nowrap;">${g['subtotal']:,.2f}/mo</td>
            </tr></table>
          </td>
        </tr>"""
        for s in g['subs']:
            sub_line = (f'<div style="font:400 12px {font};color:#8a93a6;margin-top:2px;">{escape(s["card_label"])}</div>'
                        if s.get("card_label") else "")
            sections += f"""
        <tr>
          <td style="padding:11px 0;border-top:1px solid #eef1f6;">
            <div style="font:600 15px {font};color:#1a2233;">{escape(s['name'])}</div>
            {sub_line}
          </td>
          <td align="right" style="padding:11px 0;border-top:1px solid #eef1f6;white-space:nowrap;
              font:700 15px {font};color:#1a2233;">${s['amount']:,.2f}<span style="font:400 12px {font};color:#8a93a6;">/mo</span></td>
        </tr>"""

    preheader = f"You're paying ${monthly_total:,.2f}/month across {n} active subscription{'s' if n != 1 else ''}."
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#eef1f6;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:#eef1f6;">{preheader}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#eef1f6;">
    <tr><td align="center" style="padding:24px 12px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"
             style="width:600px;max-width:100%;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #e1e6f0;">
        <tr><td style="background:#1a3c8a;padding:20px 24px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
            <td style="font:700 18px {font};color:#ffffff;">&#129689;&nbsp; Dimes</td>
            <td align="right" style="font:600 12px {font};color:#aac4ff;letter-spacing:.04em;text-transform:uppercase;">Subscriptions</td>
          </tr></table>
        </td></tr>
        <tr><td style="padding:24px 24px 6px;">
          <div style="font:700 19px {font};color:#1a2233;">Your active subscriptions</div>
          <div style="font:400 14px {font};color:#5b6472;margin-top:6px;line-height:1.5;">
            A monthly check-in on what you're subscribed to and what it costs.
          </div>
        </td></tr>
        <tr><td style="padding:6px 24px 0;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{sections}</table>
        </td></tr>
        <tr><td style="padding:16px 24px 4px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
                 style="background:#f4f7fc;border:1px solid #e1e6f0;border-radius:10px;"><tr>
            <td style="padding:14px 16px;font:600 14px {font};color:#5b6472;">Total
              <div style="font:400 12px {font};color:#8a93a6;">&#8776; ${yearly:,.0f}/year</div>
            </td>
            <td align="right" style="padding:14px 16px;font:800 22px {font};color:#1a2233;white-space:nowrap;">
              ${monthly_total:,.2f}<span style="font:400 13px {font};color:#8a93a6;">/mo</span></td>
          </tr></table>
        </td></tr>
        <tr><td style="padding:8px 24px 26px;">
          <div style="border-top:1px solid #eef1f6;padding-top:16px;font:400 12px {font};color:#98a2b3;line-height:1.6;">
            Sent monthly by <a href="https://dimes.trentschroeder.com" style="color:#1a3c8a;text-decoration:none;">Dimes</a>
            to keep your subscriptions visible. Manage them anytime from the Subscriptions page.
            {_unsubscribe_html(unsubscribe_url, font)}
          </div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = gmail_user
    msg['To'] = recipient
    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, recipient, msg.as_string())
