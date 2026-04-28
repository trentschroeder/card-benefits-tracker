import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def send_reminder_email(gmail_user, gmail_app_password, recipient, benefits_due):
    """
    benefits_due: list of dicts with keys:
      card_name, benefit_name, credit_amount,
      amount_used, period_end, days_left
    """
    if not benefits_due:
        return

    subject = f"Credit Card Benefits Reminder — {len(benefits_due)} benefit(s) need attention"

    html_rows = ""
    for b in benefits_due:
        card_label = b['card_name']

        if b['credit_amount']:
            remaining = b['credit_amount'] - (b['amount_used'] or 0)
            usage_str = f"${remaining:.2f} of ${b['credit_amount']:.2f} remaining"
        else:
            usage_str = "Not yet used"

        days_left = b['days_left']
        urgency_color = "#c0392b" if days_left <= 3 else ("#e67e22" if days_left <= 7 else "#27ae60")

        html_rows += f"""
        <tr>
          <td style="padding:8px 12px;">{card_label}</td>
          <td style="padding:8px 12px;font-weight:600;">{b['benefit_name']}</td>
          <td style="padding:8px 12px;">{usage_str}</td>
          <td style="padding:8px 12px;">Ends {b['period_end']}</td>
          <td style="padding:8px 12px;color:{urgency_color};font-weight:600;">{days_left} day(s) left</td>
        </tr>
        """

    html = f"""
    <html><body style="font-family:sans-serif;color:#333;">
    <h2 style="color:#1a5c8a;">Credit Card Benefits Reminder</h2>
    <p>The following benefits have unused credit and a reminder is due:</p>
    <table style="border-collapse:collapse;width:100%;max-width:800px;">
      <thead>
        <tr style="background:#e9ecef;text-align:left;">
          <th style="padding:8px 12px;">Card</th>
          <th style="padding:8px 12px;">Benefit</th>
          <th style="padding:8px 12px;">Remaining</th>
          <th style="padding:8px 12px;">Period End</th>
          <th style="padding:8px 12px;">Urgency</th>
        </tr>
      </thead>
      <tbody>{html_rows}</tbody>
    </table>
    <p style="margin-top:20px;font-size:0.9em;color:#666;">
      Log in to your <a href="http://localhost:5001">Credit Card Benefits</a> dashboard to record usage.
    </p>
    </body></html>
    """

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = gmail_user
    msg['To'] = recipient
    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, recipient, msg.as_string())


def send_summary_email(gmail_user, gmail_app_password, recipient, cards_data):
    """
    cards_data: list of dicts with keys:
      card_name, benefits (list of enriched benefit dicts)
    Only non-fully-used, non-subscription benefits are included.
    """
    from datetime import date
    today = date.today().strftime('%B %d, %Y')

    total_outstanding = sum(len(c['benefits']) for c in cards_data)

    card_blocks = ""
    for card in cards_data:
        if not card['benefits']:
            continue

        card_label = card['card_name']

        rows = ""
        for b in card['benefits']:
            dl = b['days_left']
            urgency_color = "#c0392b" if dl <= 3 else ("#e67e22" if dl <= 7 else "#2c3e50")
            dl_label = f"{dl}d left"

            if b['credit_amount']:
                remaining_str = f"${b['remaining']:.2f} of ${b['credit_amount']:.2f}"
                bar_pct = b['pct_used']
                bar_color = "#dc3545" if bar_pct == 0 else ("#e67e22" if bar_pct < 50 else "#27ae60")
                usage_cell = f"""
                  <div style="font-size:13px;">{remaining_str} remaining</div>
                  <div style="background:#e9ecef;border-radius:3px;height:5px;margin-top:4px;width:120px;">
                    <div style="background:{bar_color};height:5px;border-radius:3px;width:{bar_pct}%;"></div>
                  </div>"""
            else:
                usage_cell = '<span style="color:#c0392b;font-size:13px;">Not yet used</span>'

            rows += f"""
            <tr style="border-bottom:1px solid #f0f0f0;">
              <td style="padding:8px 12px;font-weight:600;font-size:13px;">{b['name']}</td>
              <td style="padding:8px 12px;font-size:12px;color:#666;">{b['period_label']}<br>ends {b['period_end'].strftime('%b %d')}</td>
              <td style="padding:8px 12px;">{usage_cell}</td>
              <td style="padding:8px 12px;font-weight:700;font-size:13px;color:{urgency_color};white-space:nowrap;">{dl_label}</td>
            </tr>"""

        card_blocks += f"""
        <div style="margin-bottom:28px;">
          <div style="font-size:15px;font-weight:700;color:#1a3c8a;padding:8px 12px;
                      background:#f0f4ff;border-left:4px solid #1a3c8a;border-radius:2px;">
            {card_label}
          </div>
          <table style="width:100%;border-collapse:collapse;">
            <thead>
              <tr style="background:#f8f9fa;font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.05em;">
                <th style="padding:6px 12px;text-align:left;">Benefit</th>
                <th style="padding:6px 12px;text-align:left;">Period</th>
                <th style="padding:6px 12px;text-align:left;">Remaining</th>
                <th style="padding:6px 12px;text-align:left;">Deadline</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </div>"""

    html = f"""
    <html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#333;max-width:680px;margin:0 auto;">
      <div style="padding:24px 0 8px;">
        <h2 style="margin:0;font-size:20px;color:#1a3c8a;">
          Credit Card Benefits Summary
        </h2>
        <p style="color:#666;font-size:13px;margin:6px 0 20px;">
          {today} &mdash; {total_outstanding} outstanding benefit(s) across {len(cards_data)} card(s)
        </p>
      </div>
      {card_blocks}
      <p style="font-size:12px;color:#999;border-top:1px solid #eee;padding-top:12px;margin-top:8px;">
        Sent from your <a href="http://localhost:5001" style="color:#1a3c8a;">Card Benefits</a> dashboard.
      </p>
    </body></html>
    """

    subject = f"Benefits Summary — {total_outstanding} outstanding ({today})"

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = gmail_user
    msg['To'] = recipient
    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, recipient, msg.as_string())
