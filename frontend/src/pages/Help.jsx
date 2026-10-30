import { Link } from 'react-router-dom';
import { getToken } from '../lib/api.js';

// User guide. Public, so people can read it before signing in. Keep the
// numbers here in sync with the backend (check times, 3-hour repeat window,
// default ±5% rule, sign-in link expiry, story age groups).
export default function Help() {
  const signedIn = !!getToken();

  return (
    <div className="container help">
      <div className="card">
        <h2>How AI Stock Watcher works</h2>
        <p>
          AI Stock Watcher keeps an eye on the stocks you care about and emails
          you when something worth knowing happens, such as a big price move
          or a new yearly high. You don't need to check prices yourself.
        </p>
        <nav aria-label="On this page" className="help-toc">
          <a href="#start">Getting started</a>
          <a href="#dashboard">Your dashboard</a>
          <a href="#alerts">Alerts and rules</a>
          <a href="#stories">Stock Stories for kids</a>
          <a href="#emails">Emails</a>
          <a href="#faq">Common questions</a>
        </nav>
      </div>

      <section className="card" id="start" aria-labelledby="start-h">
        <h3 id="start-h">Getting started</h3>
        <ol className="help-steps">
          <li>
            <strong>Sign in with your email.</strong> There's no password: enter
            your email and we'll send you a sign-in link. It works for 15
            minutes.
          </li>
          <li>
            <strong>Add stocks to your watchlist.</strong> Search by symbol or
            company name, or type a symbol directly (for example <code>TSLA</code>)
            and press <em>Add</em>.
          </li>
          <li>
            <strong>That's it.</strong> Every stock you add starts with an alert
            that emails you if it moves <strong>more than 5% up or down</strong>.
            You can change or add alerts any time with the <em>Rules</em> button.
          </li>
        </ol>
        {!signedIn && (
          <p>
            <Link to="/">Sign in to get started →</Link>
          </p>
        )}
      </section>

      <section className="card" id="dashboard" aria-labelledby="dashboard-h">
        <h3 id="dashboard-h">Your dashboard</h3>
        <p>Each stock on your watchlist shows:</p>
        <dl className="help-terms">
          <dt>Last price</dt>
          <dd>The most recent price we've recorded.</dd>
          <dt>% change</dt>
          <dd>
            How much the price has moved since the previous trading day's close.
            Green is up, red is down.
          </dd>
          <dt>Flags</dt>
          <dd>
            Shown when today's price breaks a recent record:
            <ul>
              <li><strong>W-Hi / W-Lo</strong>: new high or low for the past week</li>
              <li><strong>M-Hi / M-Lo</strong>: past month</li>
              <li><strong>Q-Hi / Q-Lo</strong>: past quarter (3 months)</li>
              <li><strong>Y-Hi / Y-Lo</strong>: past year</li>
            </ul>
          </dd>
          <dt>Alerts</dt>
          <dd>
            When this stock will email you, for example <em>🔔 Moves below −5% or
            above +5%</em>. <em>No alerts set</em> means you won't get emails for
            that stock.
          </dd>
        </dl>
        <p>
          Use <strong>Sort by</strong> to see your <em>biggest movers</em>,
          <em> top gainers</em>, <em>top losers</em>, or sort alphabetically.
          Your choice is remembered on that device.
        </p>
        <p>
          Dashboard prices refresh <strong>three times each US trading day</strong>:
          9:35 AM, 12:30 PM and 4:05 PM Eastern, Monday to Friday. A stock you've
          just added gets its price within a few seconds. Price-range alerts are
          checked more often (see below), so an alert email can arrive before the
          dashboard catches up.
        </p>
        <p>
          <strong>Removing a stock</strong> also deletes its alerts. If you add it
          back later, it starts again with the default ±5% alert.
        </p>
      </section>

      <section className="card" id="alerts" aria-labelledby="alerts-h">
        <h3 id="alerts-h">Alerts and rules</h3>
        <p>
          A <em>rule</em> tells us when to email you about a stock. Open a stock's
          rules with the <strong>Rules</strong> button on your dashboard. A stock
          can have several rules, and you can pause one without deleting it.
        </p>

        <h4>Price change outside a range</h4>
        <p>
          Emails you when the price moves outside the range you set, for example
          below −5% or above +5%, compared with the previous trading day's close
          (the day's overall move). It's checked <strong>every 10 minutes</strong>{' '}
          while the market is open.
        </p>

        <h4>New high or low</h4>
        <p>
          Emails you when a stock reaches a new weekly, monthly, quarterly or
          yearly high or low. These are checked <strong>three times each trading
          day</strong>: 9:35 AM, 12:30 PM and 4:05 PM Eastern.
        </p>

        <h4>How often you'll hear from us</h4>
        <p>
          To avoid flooding your inbox, you get <strong>at most one email per
          stock, per alert type, every 3 hours</strong>, even if the price keeps
          moving.
        </p>
      </section>

      <section className="card" id="stories" aria-labelledby="stories-h">
        <h3 id="stories-h">Stock Stories for kids</h3>
        <p>
          Many parents want to talk with their kids about money and investing but
          don't know where to start. When one of your stocks moves a lot compared
          with the previous day's close, your alert email can include a short{' '}
          <strong>Stock Story</strong>: what happened to the company, explained so
          you can read it together.
        </p>
        <ul>
          <li>
            <strong>Written for your kids' ages.</strong> Choose one or more age
            groups in {signedIn ? <Link to="/settings">Settings</Link> : 'Settings'}:
            little ones (3–5), kids (6–9), tweens (10–12) or teens (13–17). If you
            don't choose, stories are written for ages 6–9.
          </li>
          <li>
            <strong>A money idea and a question.</strong> Each story ends with one
            simple investing idea (like "prices go up and down every day") and a
            question to ask your child.
          </li>
          <li>
            <strong>For grown-ups too.</strong> A short summary, a quote from the
            news and links to the original articles, so you can read more.
          </li>
          <li>
            <strong>Based on trusted news.</strong> Stories only use reporting from a
            small set of well-known financial news sites, and say so honestly when
            there's no clear reason for a move.
          </li>
          <li>
            <strong>Sometimes a few minutes later.</strong> If a story isn't ready
            when your alert goes out, the alert says we're still looking, and the
            story follows as a reply in the same email thread.
          </li>
        </ul>
        <div className="flash warn" role="note">
          <strong>Please read it yourself first.</strong> Stock Stories are written
          by AI and haven't been checked by a person. AI can make mistakes, leave
          things out or reflect bias, so read each story before sharing it with your
          child and check the linked sources. Stories are for learning and family
          conversation only. They're not financial advice and never a recommendation
          to buy, sell or hold anything.
        </div>
      </section>

      <section className="card" id="emails" aria-labelledby="emails-h">
        <h3 id="emails-h">Emails</h3>
        <h4>Where alerts are sent</h4>
        <p>
          Alerts go to your <strong>notification email</strong>, which starts as
          the email you sign in with. To send them somewhere else, go to{' '}
          {signedIn ? <Link to="/settings">Settings</Link> : 'Settings'}, enter the
          new address and click the confirmation link we email to it. A few
          things to know:
        </p>
        <ul>
          <li>
            Until you confirm the new address, alerts are paused, so check the
            spelling before you send.
          </li>
          <li>You can't use an address that another account already uses.</li>
          <li>
            To protect your inbox, confirmation and sign-in emails can be re-sent
            every 30 seconds, up to five times in 10 minutes.
          </li>
        </ul>

        <h4>Turning alert emails off</h4>
        <p>
          Untick <em>Email me when my stock alerts are triggered</em> in Settings,
          or use the <em>Stop all alert emails</em> link at the bottom of any
          alert. Your watchlist and rules are kept, so you can turn alerts back on
          later. Sign-in links are always sent when you ask for one.
        </p>
      </section>

      <section className="card" id="faq" aria-labelledby="faq-h">
        <h3 id="faq-h">Common questions</h3>

        <details>
          <summary>A stock I just added shows "Updating…" or "—"</summary>
          <p>
            We fetch its price as soon as you add it, which usually takes a few
            seconds. If it still shows "—" after that, the symbol may be wrong or
            the price source may be busy; it will fill in at the next update.
          </p>
        </details>

        <details>
          <summary>I didn't get my sign-in email</summary>
          <p>
            It usually arrives within a minute. Check your spam or promotions
            folder, and check the address shown after you pressed <em>Send link</em>{' '}
            is spelled right; if it isn't, choose <em>Use a different email</em>.
            Links expire after 15 minutes; you can request a new one after 30
            seconds.
          </p>
          <p>
            It's fine to switch to your mail app straight after pressing{' '}
            <em>Send link</em>. The link is still sent.
          </p>
        </details>

        <details>
          <summary>Why didn't I get an alert?</summary>
          <ul>
            <li>The stock has <em>No alerts set</em>, or its rule is paused.</li>
            <li>Alert emails are turned off in Settings (the dashboard shows a yellow notice).</li>
            <li>Your notification email is waiting for confirmation.</li>
            <li>The market was closed: alerts are only checked on US trading days.</li>
            <li>You already got the same alert for that stock in the last 3 hours.</li>
          </ul>
        </details>

        <details>
          <summary>Why didn't my alert include a Stock Story?</summary>
          <ul>
            <li>We couldn't find a clear reason for the move in trusted news. We'd rather say nothing than guess.</li>
            <li>The move was small: under 1% for a stock, or under 0.5% for a fund (ETF or index). Small moves rarely have a news reason, so you get a short "small move" note for your kids instead.</li>
            <li>The story didn't pass our safety checks, so we didn't send it.</li>
            <li>We'd already written a lot of stories that day. The biggest moves get stories first.</li>
          </ul>
        </details>

        <details>
          <summary>Does it work on my phone?</summary>
          <p>
            Yes. The app works in any modern mobile browser, and each stock is
            shown as a compact card on small screens.
          </p>
        </details>

        <details>
          <summary>Is this financial advice?</summary>
          <p>
            No. AI Stock Watcher only tells you when prices move. It doesn't
            recommend buying or selling anything.
          </p>
        </details>
      </section>
    </div>
  );
}
