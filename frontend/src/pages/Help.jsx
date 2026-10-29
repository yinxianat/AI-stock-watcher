import { Link } from 'react-router-dom';
import { getToken } from '../lib/api.js';

// User guide. Public, so people can read it before signing in. Keep the
// numbers here in sync with the backend (check times, 3-hour repeat window,
// default ±5% rule, sign-in link expiry).
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
          below −5% or above +5%. It's checked <strong>every 10 minutes</strong>{' '}
          while the market is open, against two things:
        </p>
        <ul>
          <li>the previous trading day's close (the day's overall move), and</li>
          <li>the price about 10 minutes earlier (a sudden jump or drop).</li>
        </ul>

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
