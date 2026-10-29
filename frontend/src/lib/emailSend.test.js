import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { NetworkError, RateLimitError } from '../api/errors.js';
import { sendEmailRequest } from './emailSend.js';
import { suggestEmail } from './emailTypos.js';

describe('suggestEmail', () => {
  it('fixes common domain typos', () => {
    expect(suggestEmail('bob@gmial.com')).toBe('bob@gmail.com');
    expect(suggestEmail('Bob.Smith@Yahoo.con')).toBe('Bob.Smith@yahoo.com');
  });
  it('leaves correct or unknown domains alone', () => {
    expect(suggestEmail('bob@gmail.com')).toBeNull();
    expect(suggestEmail('bob@company.io')).toBeNull();
    expect(suggestEmail('bob')).toBeNull();
    expect(suggestEmail('@gmial.com')).toBeNull();
  });
});

describe('sendEmailRequest', () => {
  // Minimal stand-in for the browser's document (tests run in Node).
  let listeners;
  beforeEach(() => {
    listeners = [];
    globalThis.document = {
      visibilityState: 'visible',
      addEventListener: (_, fn) => listeners.push(fn),
      removeEventListener: (_, fn) => { listeners = listeners.filter((l) => l !== fn); },
    };
  });
  afterEach(() => { delete globalThis.document; });

  function leavePage() {
    document.visibilityState = 'hidden';
    listeners.forEach((fn) => fn());
  }

  it("is 'sent' when the server replies", async () => {
    expect(await sendEmailRequest(async () => null)).toBe('sent');
  });

  it("is 'sent' when the connection drops after the user switched apps", async () => {
    const send = async () => {
      leavePage();
      throw new NetworkError(new Error('Fetch is aborted'));
    };
    expect(await sendEmailRequest(send)).toBe('sent');
  });

  it("is 'unsure' when the connection drops while the user is still here", async () => {
    const send = async () => { throw new NetworkError(new Error('Load failed')); };
    expect(await sendEmailRequest(send)).toBe('unsure');
  });

  it('rethrows real errors like rate limits', async () => {
    const send = async () => { throw new RateLimitError('Too many', { retryAfterSeconds: 20 }); };
    await expect(sendEmailRequest(send)).rejects.toBeInstanceOf(RateLimitError);
  });

  it('stops listening afterwards', async () => {
    await sendEmailRequest(async () => null);
    expect(listeners).toHaveLength(0);
  });
});
